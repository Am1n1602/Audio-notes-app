"""A recording stays in storage for at most AUDIO_RETENTION_SECONDS; the transcript and summary stay."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakes import FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import get_settings
from app.core.failures import Failure
from app.db.models import AudioJob, JobStatus
from app.providers.storage import S3Storage, StorageError
from app.services import jobs, retention
from app.services.uploads import _how_long

RETENTION = get_settings().audio_retention_seconds


def key_of(db: Session, job_id: uuid.UUID) -> str:
    return jobs.get_job(db, job_id).object_key


def age(db: Session, job_id: uuid.UUID, seconds: int) -> None:
    """Make a job look `seconds` old, whatever the clock says."""
    when = datetime.now(UTC) - timedelta(seconds=seconds)
    db.execute(update(AudioJob).where(AudioJob.id == job_id).values(created_at=when))
    db.commit()
    db.expire_all()


def deleted_at(db: Session, job_id: uuid.UUID) -> datetime | None:
    db.expire_all()
    return jobs.get_job(db, job_id).audio_deleted_at


# --- the default -----------------------------------------------------------------------------------------------------


def test_the_default_retention_is_two_hours() -> None:
    assert RETENTION == 2 * 3600


@pytest.mark.parametrize(
    ("seconds", "words"),
    [(7200, "2 hours"), (3600, "1 hour"), (120, "2 minutes"), (60, "1 minute"), (5400, "90 minutes")],
)
def test_the_retention_is_said_the_way_a_person_says_it(seconds: int, words: str) -> None:
    assert _how_long(seconds) == words


# --- deleting one recording ------------------------------------------------------------------------------------------


def test_expiring_deletes_the_file_and_notes_it_once(db: Session, storage: FakeStorage) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)
    key = key_of(db, job_id)
    storage.put(key, 100)

    assert retention.expire_recording(db, storage, job_id) is True
    assert key not in storage.objects and storage.deleted == [key]
    assert deleted_at(db, job_id) is not None

    assert retention.expire_recording(db, storage, job_id) is False  # a duplicate or late delivery does nothing
    assert storage.deleted == [key]


def test_a_storage_outage_leaves_the_job_unmarked_so_the_retry_can_finish_it(db: Session, storage: FakeStorage) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)
    storage.put(key_of(db, job_id), 100)
    storage.outage = True
    with pytest.raises(StorageError):
        retention.expire_recording(db, storage, job_id)
    assert deleted_at(db, job_id) is None  # never "deleted" while the file is still there

    storage.outage = False
    assert retention.expire_recording(db, storage, job_id) is True


def test_a_job_that_does_not_exist_is_ignored(db: Session, storage: FakeStorage) -> None:
    assert retention.expire_recording(db, storage, uuid.uuid4()) is False
    assert storage.deleted == []


def test_the_transcript_and_summary_survive_the_deletion(db: Session, storage: FakeStorage) -> None:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(
        update(AudioJob).where(AudioJob.id == job_id).values(transcript="hello world", summary={"overview": "x"})
    )
    db.commit()
    retention.expire_recording(db, storage, job_id)
    db.expire_all()
    job = jobs.get_job(db, job_id)
    assert (job.transcript, job.summary) == ("hello world", {"overview": "x"})


# --- the daily sweep -------------------------------------------------------------------------------------------------


def test_the_sweep_deletes_what_is_overdue_and_only_that(db: Session, storage: FakeStorage) -> None:
    old, fresh = job_in_status(db, JobStatus.QUEUED), job_in_status(db, JobStatus.QUEUED)
    for job_id in (old, fresh):
        storage.put(key_of(db, job_id), 100)
    age(db, old, RETENTION + 60)
    age(db, fresh, RETENTION - 600)

    assert retention.expire_overdue(db, storage, get_settings()) == (1, 0)
    assert storage.deleted == [key_of(db, old)] and key_of(db, fresh) in storage.objects
    assert deleted_at(db, old) is not None and deleted_at(db, fresh) is None
    assert retention.expire_overdue(db, storage, get_settings()) == (0, 0)  # nothing left to do


def test_the_sweep_goes_oldest_first_and_stops_at_its_batch_size(db: Session, storage: FakeStorage) -> None:
    ids = [job_in_status(db, JobStatus.QUEUED) for _ in range(3)]
    for hours, job_id in zip((5, 3, 4), ids, strict=True):
        age(db, job_id, hours * 3600)
    assert retention.expire_overdue(db, storage, get_settings(), limit=2) == (2, 0)
    assert [deleted_at(db, j) is not None for j in ids] == [True, False, True]  # the 5 h and the 4 h old, not the 3 h


def test_one_failing_delete_does_not_stop_the_rest(db: Session) -> None:
    class FlakyStorage(FakeStorage):
        def delete(self, key: str) -> None:
            if "bad" in key:
                raise StorageError("refused")
            super().delete(key)

    storage = FlakyStorage()
    bad, good = job_in_status(db, JobStatus.QUEUED), job_in_status(db, JobStatus.QUEUED)
    db.execute(update(AudioJob).where(AudioJob.id == bad).values(object_key=f"uploads/{bad}/bad.wav"))
    db.commit()
    age(db, bad, RETENTION * 3)  # the oldest, so it is tried first
    age(db, good, RETENTION * 2)

    assert retention.expire_overdue(db, storage, get_settings()) == (1, 1)
    assert deleted_at(db, bad) is None and deleted_at(db, good) is not None  # the bad one waits for the next sweep


# --- what the API does once the recording is gone --------------------------------------------------------------------


def test_the_audio_link_says_the_recording_was_deleted(api: TestClient, db: Session, storage: FakeStorage) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)
    storage.put(key_of(db, job_id), 100)
    assert api.get(f"/api/uploads/{job_id}/audio").status_code == 200

    retention.expire_recording(db, storage, job_id)
    res = api.get(f"/api/uploads/{job_id}/audio")
    assert (res.status_code, res.json()["error"]["code"]) == (410, "RECORDING_EXPIRED")
    assert "2 hours" in res.json()["error"]["message"] and "transcript and summary" in res.json()["error"]["message"]
    detail = api.get(f"/api/uploads/{job_id}").json()
    assert detail["audio_deleted_at"] is not None  # the page can say so without asking for the link


def test_an_upload_confirmed_after_its_recording_expired_is_refused(
    api: TestClient, db: Session, storage: FakeStorage, queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADING)
    retention.expire_recording(db, storage, job_id)  # nothing was there yet; the job is marked anyway
    storage.put(key_of(db, job_id), 100)  # the file lands after the deadline
    res = api.post(f"/api/uploads/{job_id}/complete")
    assert (res.status_code, res.json()["error"]["code"]) == (409, "RECORDING_EXPIRED")
    assert queue.enqueued == []  # and is not processed


def fail(db: Session, job_id: uuid.UUID, code: str) -> None:
    assert jobs.fail_job(db, job_id, Failure(code, "It failed."))


def test_a_failed_job_that_needs_the_audio_cannot_be_retried_after_the_deletion(
    api: TestClient, db: Session, storage: FakeStorage, queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    fail(db, job_id, failures.TRANSCRIPTION_TIMEOUT)
    assert api.get(f"/api/uploads/{job_id}").json()["can_retry"] is True

    retention.expire_recording(db, storage, job_id)
    assert api.get(f"/api/uploads/{job_id}").json()["can_retry"] is False
    assert {row["id"]: row["can_retry"] for row in api.get("/api/uploads").json()}[str(job_id)] is False
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert (res.status_code, res.json()["error"]["code"]) == (409, "NOT_RETRYABLE")
    assert "deleted" in res.json()["error"]["message"] and queue.enqueued == []


def test_a_summary_failure_can_still_be_retried_after_the_deletion(
    api: TestClient, db: Session, storage: FakeStorage, queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(update(AudioJob).where(AudioJob.id == job_id).values(transcript="hello world"))
    db.commit()
    fail(db, job_id, failures.SUMMARY_UNAVAILABLE)
    retention.expire_recording(db, storage, job_id)

    assert api.get(f"/api/uploads/{job_id}").json()["can_retry"] is True  # it only reads the saved transcript
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert res.status_code == 200 and res.json()["status"] == "SUMMARIZING"
    assert queue.enqueued == [(job_id, 0)]


@pytest.mark.parametrize("code", [failures.TRANSCRIPTION_TIMEOUT, failures.INTERNAL_ERROR, failures.QUEUE_UNAVAILABLE])
def test_a_crash_after_the_transcript_was_saved_stays_retryable_after_the_deletion(
    api: TestClient, db: Session, storage: FakeStorage, queue: FakeQueue, code: str
) -> None:
    """The Retry button must not disappear for a retry that would work: a transcript is saved, so resuming at the
    summary never needs the deleted recording, whatever the failure's code says (a crash or a dead queue can happen
    there)."""
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(update(AudioJob).where(AudioJob.id == job_id).values(transcript="hello world"))
    db.commit()
    fail(db, job_id, code)
    retention.expire_recording(db, storage, job_id)
    assert api.get(f"/api/uploads/{job_id}").json()["can_retry"] is True
    assert api.post(f"/api/uploads/{job_id}/retry").status_code == 200


@pytest.mark.parametrize("audio_deleted", [False, True])
@pytest.mark.parametrize("has_transcript", [False, True])
@pytest.mark.parametrize(
    "code",
    [
        failures.TRANSCRIPTION_TIMEOUT,
        failures.INTERNAL_ERROR,
        failures.QUEUE_UNAVAILABLE,
        failures.SUMMARY_UNAVAILABLE,
        failures.EMPTY_TRANSCRIPT,  # never retryable
    ],
)
def test_the_retry_button_is_offered_exactly_when_retry_would_be_accepted(
    api: TestClient, db: Session, storage: FakeStorage, code: str, has_transcript: bool, audio_deleted: bool
) -> None:
    """can_retry (schema) and retry_upload (service) are two statements of one rule. Every combination must agree, in
    the detail, in the list, and with what the retry endpoint then really does."""
    job_id = job_in_status(db, JobStatus.SUMMARIZING if has_transcript else JobStatus.TRANSCRIBING)
    if has_transcript:
        db.execute(update(AudioJob).where(AudioJob.id == job_id).values(transcript="hello world"))
        db.commit()
    fail(db, job_id, code)
    if audio_deleted:
        retention.expire_recording(db, storage, job_id)

    offered = api.get(f"/api/uploads/{job_id}").json()["can_retry"]
    assert {row["id"]: row["can_retry"] for row in api.get("/api/uploads").json()}[str(job_id)] is offered
    assert api.post(f"/api/uploads/{job_id}/retry").status_code == (200 if offered else 409)


def test_a_failed_statement_does_not_stop_the_rest_of_the_sweep(
    db: Session, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a database error Postgres refuses every further statement on that connection until a rollback. Without one,
    each remaining job in the batch would be counted as failed without being tried."""
    first, second = job_in_status(db, JobStatus.QUEUED), job_in_status(db, JobStatus.QUEUED)
    age(db, first, RETENTION * 3)  # the oldest: tried first
    age(db, second, RETENTION * 2)
    real, calls = jobs.mark_audio_deleted, {"n": 0}

    def broken_once(session: Session, job_id: uuid.UUID) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            session.execute(text("SELECT * FROM no_such_table"))  # fails, and aborts the transaction
        return real(session, job_id)

    monkeypatch.setattr(jobs, "mark_audio_deleted", broken_once)
    assert retention.expire_overdue(db, storage, get_settings()) == (1, 1)
    assert deleted_at(db, first) is None and deleted_at(db, second) is not None  # the first waits for the next sweep


# --- scheduling it at /initiate --------------------------------------------------------------------------------------


def test_every_upload_gets_its_deletion_scheduled_at_the_retention(api: TestClient, queue: FakeQueue) -> None:
    init = api.post("/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": 100}).json()
    assert queue.expirations == [(uuid.UUID(init["id"]), RETENTION)]
    assert queue.enqueued == []  # nothing is processed until /complete


def test_an_upload_whose_deletion_cannot_be_scheduled_is_refused_and_leaves_nothing_behind(
    api: TestClient, db: Session, queue: FakeQueue
) -> None:
    queue.outage = True
    res = api.post("/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": 100})
    assert (res.status_code, res.json()["error"]["code"]) == (503, "QUEUE_UNAVAILABLE")
    assert db.scalars(select(AudioJob)).all() == []  # no row, so no job a person could wait on


def test_the_config_tells_the_page_how_long_recordings_are_kept(api: TestClient) -> None:
    assert api.get("/api/config").json()["audio_retention_seconds"] == RETENTION


# --- the storage adapter ---------------------------------------------------------------------------------------------


class RecordingS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return {}


def test_the_s3_adapter_deletes_the_named_object() -> None:
    client = RecordingS3()
    S3Storage(client, "the-bucket").delete("uploads/abc/audio.wav")
    assert client.calls == [{"Bucket": "the-bucket", "Key": "uploads/abc/audio.wav"}]


def test_an_s3_refusal_or_outage_is_a_storage_error() -> None:
    from botocore.exceptions import ClientError, EndpointConnectionError

    refused = ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
    for error in (refused, EndpointConnectionError(endpoint_url="https://s3.test")):
        with pytest.raises(StorageError):
            S3Storage(RecordingS3(error), "b").delete("k")
