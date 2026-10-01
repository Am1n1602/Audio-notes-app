import time
import uuid
from typing import Any

import pytest
from fakes import FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import get_settings
from app.core.failures import Failure
from app.db.models import JobStatus
from app.providers.queue import (
    EXPIRE_RECORDING_TASK,
    PROCESS_JOB_TASK,
    CeleryJobQueue,
    QueueError,
    make_celery_app,
)
from app.providers.storage import StorageError
from app.services import jobs, pipeline, steps
from worker import tasks


def failed_job(db: Session, code: str) -> str:
    job_id = job_in_status(db, JobStatus.UPLOADED)
    assert jobs.fail_job(db, job_id, Failure(code, "It failed."))
    return str(job_id)


def body(res: Any) -> dict[str, Any]:
    return res.json()  # type: ignore[no-any-return]


# --- queue outage at /complete ---------------------------------------------------------------------------------


def test_a_dead_queue_fails_the_job_visibly_and_retry_recovers_it(
    api: TestClient, storage: FakeStorage, queue: FakeQueue
) -> None:
    init = api.post("/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": 100}).json()
    storage.put(f"uploads/{init['id']}/audio.wav", 100)
    queue.outage = True

    res = api.post(f"/api/uploads/{init['id']}/complete")
    assert (res.status_code, body(res)["error"]["code"]) == (503, "QUEUE_UNAVAILABLE")
    job = body(api.get(f"/api/uploads/{init['id']}"))
    assert (job["status"], job["error_code"], job["can_retry"]) == ("FAILED", "QUEUE_UNAVAILABLE", True)
    assert "Retry" in job["error_message"] and job["progress_message"] is None

    queue.outage = False  # Redis is back
    retried = body(api.post(f"/api/uploads/{init['id']}/retry"))
    assert (retried["status"], retried["error_code"], retried["can_retry"]) == ("QUEUED", None, False)
    assert queue.enqueued == [(uuid.UUID(init["id"]), 0)]  # the file was already in storage: no re-upload needed


def test_retry_that_hits_a_dead_queue_fails_again_and_stays_retryable(
    api: TestClient, db: Session, queue: FakeQueue
) -> None:
    job_id = failed_job(db, failures.TRANSCRIPTION_UNAVAILABLE)
    queue.outage = True
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert (res.status_code, body(res)["error"]["code"]) == (503, "QUEUE_UNAVAILABLE")
    assert body(api.get(f"/api/uploads/{job_id}"))["can_retry"] is True


# --- the retry endpoint ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(failures.RETRYABLE_CODES))
def test_every_retryable_failure_can_be_retried(api: TestClient, db: Session, queue: FakeQueue, code: str) -> None:
    job_id = failed_job(db, code)
    assert body(api.get(f"/api/uploads/{job_id}"))["can_retry"] is True
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert res.status_code == 200 and body(res)["status"] == "QUEUED"
    assert len(queue.enqueued) == 1


@pytest.mark.parametrize(
    "code",
    [
        failures.UPLOAD_SIZE_MISMATCH,
        failures.EMPTY_TRANSCRIPT,
        failures.INVALID_AUDIO,  # pressing Retry on a non-audio file can never help
        failures.RECORDING_TOO_LONG,  # nor on a file that is over the length limit
        failures.RECORDING_MISSING,  # nor on one that is gone from storage
        failures.TRANSCRIPTION_AUTH_FAILED,
        failures.TRANSCRIPTION_REQUEST_REJECTED,
    ],
)
def test_failures_that_retrying_cannot_fix_are_refused(
    api: TestClient, db: Session, queue: FakeQueue, code: str
) -> None:
    job_id = failed_job(db, code)
    assert body(api.get(f"/api/uploads/{job_id}"))["can_retry"] is False
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert (res.status_code, body(res)["error"]["code"]) == (409, "NOT_RETRYABLE")
    assert queue.enqueued == []


def test_retry_twice_starts_one_new_run(api: TestClient, db: Session, queue: FakeQueue) -> None:
    job_id = failed_job(db, failures.TRANSCRIPTION_TIMEOUT)
    first, second = (api.post(f"/api/uploads/{job_id}/retry") for _ in range(2))
    assert (
        first.status_code == second.status_code == 200 and body(first)["status"] == body(second)["status"] == "QUEUED"
    )
    assert len(queue.enqueued) == 1


def test_retry_only_applies_to_failed_jobs(api: TestClient, db: Session, queue: FakeQueue) -> None:
    running = job_in_status(db, JobStatus.TRANSCRIBING)
    res = api.post(f"/api/uploads/{running}/retry")  # already running: harmless no-op, nothing enqueued
    assert res.status_code == 200 and body(res)["status"] == "TRANSCRIBING"
    uploading = job_in_status(db, JobStatus.UPLOADING)
    assert body(api.post(f"/api/uploads/{uploading}/retry"))["error"]["code"] == "NOT_RETRYABLE"
    assert body(api.post(f"/api/uploads/{uuid.uuid4()}/retry"))["error"]["code"] == "JOB_NOT_FOUND"
    assert queue.enqueued == []


def test_the_list_marks_which_rows_offer_a_retry(api: TestClient, db: Session) -> None:
    retryable, final = failed_job(db, failures.TRANSCRIPTION_TIMEOUT), failed_job(db, failures.EMPTY_TRANSCRIPT)
    rows: list[dict[str, Any]] = api.get("/api/uploads").json()
    flags = {row["id"]: row["can_retry"] for row in rows}
    assert flags[retryable] is True and flags[final] is False


# --- the queue adapter ------------------------------------------------------------------------------------------


def test_an_unreachable_broker_fails_fast_with_our_error_type() -> None:
    dead = get_settings().model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    started = time.perf_counter()
    with pytest.raises(QueueError):
        CeleryJobQueue(make_celery_app(dead)).enqueue_process_job(uuid.uuid4())
    assert time.perf_counter() - started < 15  # bounded: /complete must answer even when Redis is down


def test_celery_is_configured_for_short_idempotent_at_least_once_tasks() -> None:
    conf = make_celery_app(get_settings()).conf
    assert conf.task_acks_late is True and conf.worker_prefetch_multiplier == 1
    assert conf.worker_cancel_long_running_tasks_on_connection_loss is False  # explicit, see queue.py
    assert conf.result_backend is None  # results live in Postgres
    assert conf.broker_transport_options == {"visibility_timeout": 300}  # a killed worker's step returns in 5 minutes


# --- the Celery task ---------------------------------------------------------------------------------------------


@pytest.fixture
def worker_queue(monkeypatch: pytest.MonkeyPatch, db: Session) -> FakeQueue:
    fake = FakeQueue()
    monkeypatch.setattr(tasks, "queue", fake)
    return fake


def test_the_task_has_the_name_the_api_sends() -> None:
    assert tasks.process_job.name == PROCESS_JOB_TASK == "worker.process_job"


def test_a_step_that_is_not_finished_reschedules_itself(
    monkeypatch: pytest.MonkeyPatch, worker_queue: FakeQueue
) -> None:
    monkeypatch.setattr(pipeline, "process_job", lambda *a, **k: 10)
    job_id = uuid.uuid4()
    assert tasks.process_job.apply(args=[str(job_id)]).successful()
    assert worker_queue.enqueued == [(job_id, 10)]


def test_a_finished_step_does_not_reschedule(monkeypatch: pytest.MonkeyPatch, worker_queue: FakeQueue) -> None:
    monkeypatch.setattr(pipeline, "process_job", lambda *a, **k: None)
    assert tasks.process_job.apply(args=[str(uuid.uuid4())]).successful()
    assert worker_queue.enqueued == []


def test_a_job_that_no_longer_exists_is_skipped_quietly(worker_queue: FakeQueue) -> None:
    assert tasks.process_job.apply(args=[str(uuid.uuid4())]).successful()
    assert worker_queue.enqueued == []


def test_a_crash_is_recorded_on_the_job_and_still_raised(
    monkeypatch: pytest.MonkeyPatch, db: Session, worker_queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("internal detail /srv/secret")

    monkeypatch.setattr(pipeline, "process_job", boom)
    result = tasks.process_job.apply(args=[str(job_id)])
    assert result.failed() and isinstance(result.result, RuntimeError)  # not swallowed
    db.expire_all()
    job = jobs.get_job(db, job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.INTERNAL_ERROR)
    assert "secret" not in (job.error_message or "")  # the user-visible text never carries exception detail
    assert job.error_code in failures.RETRYABLE_CODES and worker_queue.enqueued == []


def test_a_database_blip_is_retried_by_celery_not_recorded_as_a_job_failure(
    monkeypatch: pytest.MonkeyPatch, db: Session, worker_queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    calls = {"n": 0}

    def flaky(*_a: Any, **_k: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return 10

    monkeypatch.setattr(pipeline, "process_job", flaky)
    assert tasks.process_job.apply(args=[str(job_id)]).successful()
    assert calls["n"] == 2 and worker_queue.enqueued == [(job_id, 10)]
    db.expire_all()
    assert jobs.get_job(db, job_id).status is JobStatus.TRANSCRIBING  # a blip must not fail the job


def test_infrastructure_retries_are_bounded() -> None:
    assert tasks.process_job.autoretry_for == (OperationalError, QueueError)
    assert tasks.process_job.max_retries == 10 and tasks.process_job.retry_backoff_max == 120


# --- the deletion task -------------------------------------------------------------------------------------------


def test_the_expiry_task_has_the_name_the_api_sends() -> None:
    assert tasks.expire_recording.name == EXPIRE_RECORDING_TASK == "worker.expire_recording"


def test_the_expiry_task_deletes_through_the_shared_runner(
    monkeypatch: pytest.MonkeyPatch, db: Session, storage: FakeStorage
) -> None:
    monkeypatch.setattr(steps, "get_storage", lambda: storage)
    job_id = job_in_status(db, JobStatus.QUEUED)
    key = jobs.get_job(db, job_id).object_key
    storage.put(key, 100)
    assert tasks.expire_recording.apply(args=[str(job_id)]).successful()
    assert storage.deleted == [key]


def test_a_storage_outage_is_retried_by_celery_with_the_same_bounds_as_a_step() -> None:
    assert tasks.expire_recording.autoretry_for == (OperationalError, StorageError)
    assert tasks.expire_recording.max_retries == 10 and tasks.expire_recording.retry_backoff_max == 120


def test_an_unreachable_broker_fails_the_deletion_enqueue_fast_with_our_error_type() -> None:
    dead = get_settings().model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})
    with pytest.raises(QueueError):
        CeleryJobQueue(make_celery_app(dead)).enqueue_expire_recording(uuid.uuid4(), 7200)
