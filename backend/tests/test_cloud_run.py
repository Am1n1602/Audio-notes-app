"""Phase 8: what changes when the app runs on Cloud Run + Cloud Tasks instead of Redis + Celery."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from fakes import FakeQueue
from fastapi.testclient import TestClient
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import tasks_v2
from helpers import OWNER_A, job_in_status
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings, get_settings
from app.db.models import AudioJob, JobStatus
from app.main import create_app
from app.providers.cloud_tasks import CloudTasksJobQueue
from app.providers.queue import QueueError, get_queue
from app.services import jobs, pipeline

QUEUE = "projects/demo/locations/asia-south1/queues/job-steps"
STEPS = "https://steps-123.asia-south1.run.app"
INVOKER = "steps-sa@demo.iam.gserviceaccount.com"


def cloud_settings(**override: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://u:p@h/db",
        queue_backend="cloudtasks",
        cloud_tasks_queue=QUEUE,
        steps_url=STEPS + "/",  # a trailing slash must not produce //internal/steps
        steps_invoker_email=INVOKER,
        **override,
    )


# --- configuration ------------------------------------------------------------------------------------------------


def test_the_cloud_tasks_backend_names_every_setting_it_is_missing() -> None:
    with pytest.raises(ValueError, match="CLOUD_TASKS_QUEUE, STEPS_URL, STEPS_INVOKER_EMAIL"):
        Settings(_env_file=None, database_url="postgresql://u:p@h/db", queue_backend="cloudtasks")


def test_the_celery_backend_needs_redis_and_cloud_tasks_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL")  # the root conftest sets it
    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings(_env_file=None, database_url="postgresql://u:p@h/db")
    assert cloud_settings().redis_url == ""


# --- the Cloud Tasks queue ----------------------------------------------------------------------------------------


class FakeTasksClient:
    """Stands in for CloudTasksClient. It builds the real request message too, which rejects a misspelt field."""

    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[tasks_v2.CreateTaskRequest] = []
        self.timeouts: list[float] = []
        self.error = error

    def create_task(self, request: dict[str, Any], timeout: float) -> None:
        if self.error:
            raise self.error
        self.requests.append(tasks_v2.CreateTaskRequest(request))
        self.timeouts.append(timeout)


def test_a_step_becomes_an_authenticated_post_to_the_private_service() -> None:
    client = FakeTasksClient()
    job_id = uuid.uuid4()
    CloudTasksJobQueue(cloud_settings(), client).enqueue_process_job(job_id)

    (request,) = client.requests
    http = request.task.http_request
    assert request.parent == QUEUE
    assert http.http_method == tasks_v2.HttpMethod.POST
    assert http.url == f"{STEPS}/internal/steps"
    assert json.loads(http.body) == {"job_id": str(job_id)}
    assert http.headers["Content-Type"] == "application/json"
    # The token proves to Cloud Run who is calling; its audience must be the service URL, not the path.
    assert (http.oidc_token.service_account_email, http.oidc_token.audience) == (INVOKER, STEPS)
    assert request.task.dispatch_deadline.seconds == 300  # above the longest step (about 215 s)
    assert not request.task.schedule_time  # no countdown: due immediately
    assert client.timeouts == [10]


def test_the_countdown_becomes_the_schedule_time() -> None:
    client = FakeTasksClient()
    before = datetime.now(UTC)
    CloudTasksJobQueue(cloud_settings(), client).enqueue_process_job(uuid.uuid4(), countdown=30)
    due = client.requests[0].task.schedule_time.timestamp()
    assert 29 <= due - before.timestamp() <= 35


@pytest.mark.parametrize(
    "error_type", [ServiceUnavailable, DeadlineExceeded, DefaultCredentialsError, requests.ConnectionError]
)
def test_a_queue_outage_surfaces_as_our_error_type(error_type: type[Exception]) -> None:
    queue = CloudTasksJobQueue(cloud_settings(), FakeTasksClient(error_type("simulated")))
    with pytest.raises(QueueError):  # /complete answers 503 QUEUE_UNAVAILABLE and the step endpoint answers 503
        queue.enqueue_process_job(uuid.uuid4())


def test_get_queue_builds_the_adapter_the_backend_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.providers.queue.get_settings", lambda: cloud_settings())
    get_queue.cache_clear()
    try:
        assert isinstance(get_queue(), CloudTasksJobQueue)  # building it needs no Google credentials
    finally:
        get_queue.cache_clear()


def test_missing_google_credentials_fail_the_enqueue_not_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_credentials(**_: Any) -> None:
        raise DefaultCredentialsError  # the class itself: mypy sees its constructor as untyped

    monkeypatch.setattr(tasks_v2, "CloudTasksClient", no_credentials)
    with pytest.raises(QueueError):
        CloudTasksJobQueue(cloud_settings()).enqueue_process_job(uuid.uuid4())


# --- the private step endpoint ------------------------------------------------------------------------------------


@pytest.fixture
def steps_client(monkeypatch: pytest.MonkeyPatch, db: Session, queue: FakeQueue) -> Iterator[TestClient]:
    """The app as the private `steps` service builds it, with the queue faked."""
    monkeypatch.setenv("SERVICE_ROLE", "steps")
    get_settings.cache_clear()
    try:
        app = create_app()
    finally:
        get_settings.cache_clear()  # nothing else in the suite wants the steps-role settings
    app.dependency_overrides[get_queue] = lambda: queue
    yield TestClient(app, raise_server_exceptions=False)


def post_step(client: TestClient, job_id: uuid.UUID | str) -> Any:
    return client.post("/internal/steps", json={"job_id": str(job_id)})


def test_the_two_services_expose_different_routes(steps_client: TestClient, api: TestClient) -> None:
    assert steps_client.get("/api/health").status_code == 404  # the private service has no public routes
    assert steps_client.get("/api/uploads").status_code == 404
    assert post_step(api, uuid.uuid4()).status_code == 404  # and the public one cannot be asked to run a step


def test_the_steps_service_refuses_to_start_without_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_ROLE", "steps")
    monkeypatch.setenv("GNANI_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="GNANI_API_KEY"):
            create_app()
    finally:
        get_settings.cache_clear()


def test_a_step_that_has_more_to_do_enqueues_the_next_one(
    steps_client: TestClient, db: Session, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "process_job", lambda *a, **k: 10)
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    res = post_step(steps_client, job_id)
    assert (res.status_code, res.json()) == (200, {"status": "done"})
    assert queue.enqueued == [(job_id, 10)]
    db.expire_all()
    assert jobs.get_job(db, job_id).next_step_at is not None  # recorded before the enqueue (see services/steps.py)


def test_a_step_with_nothing_more_to_do_enqueues_nothing(
    steps_client: TestClient, db: Session, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "process_job", lambda *a, **k: None)
    assert post_step(steps_client, job_in_status(db, JobStatus.SUMMARIZING)).status_code == 200
    assert queue.enqueued == []


def test_a_step_for_a_job_that_no_longer_exists_is_acknowledged(steps_client: TestClient, queue: FakeQueue) -> None:
    res = post_step(steps_client, uuid.uuid4())  # not patched: the real pipeline looks the job up and finds nothing
    assert (res.status_code, res.json()) == (200, {"status": "done"})  # 2xx, or Cloud Tasks redelivers it for ever
    assert queue.enqueued == []


def test_a_database_blip_asks_cloud_tasks_to_deliver_the_step_again(
    steps_client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blip(*_a: Any, **_k: Any) -> None:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    monkeypatch.setattr(pipeline, "process_job", blip)
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    res = post_step(steps_client, job_id)
    assert (res.status_code, res.json()["error"]["code"]) == (503, "STEP_RETRY")
    db.expire_all()
    assert jobs.get_job(db, job_id).status is JobStatus.TRANSCRIBING  # a blip must not fail the job


def test_a_failed_enqueue_asks_for_redelivery_and_leaves_no_wrong_due_time(
    steps_client: TestClient, db: Session, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "process_job", lambda *a, **k: 10)
    queue.outage = True
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    assert post_step(steps_client, job_id).status_code == 503
    db.expire_all()
    assert jobs.get_job(db, job_id).next_step_at is None  # nothing is scheduled, so the redelivered step must run


def test_a_bug_is_recorded_on_the_job_and_acknowledged(
    steps_client: TestClient, db: Session, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("internal detail /srv/secret")

    monkeypatch.setattr(pipeline, "process_job", boom)
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    res = post_step(steps_client, job_id)
    assert (res.status_code, res.json()) == (200, {"status": "failed"})  # redelivery would only repeat the crash
    db.expire_all()
    job = jobs.get_job(db, job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.INTERNAL_ERROR)
    assert "secret" not in (job.error_message or "") and queue.enqueued == []


def test_a_malformed_request_is_refused(steps_client: TestClient) -> None:
    assert steps_client.post("/internal/steps", json={"job_id": "not-a-uuid"}).status_code == 422
    assert steps_client.post("/internal/steps", json={}).status_code == 422


# --- the health check without Redis -------------------------------------------------------------------------------


def test_health_does_not_probe_redis_when_the_queue_is_cloud_tasks(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import app
    from app.services import health

    monkeypatch.setattr(health, "check_redis", lambda _url: pytest.fail("Redis must not be probed"))
    app.dependency_overrides[get_settings] = lambda: cloud_settings()
    res = api.get("/api/health")
    assert (res.status_code, res.json()) == (200, {"status": "ok", "db": "ok", "redis": None})


# --- the per-browser daily upload limit ---------------------------------------------------------------------------


@pytest.fixture
def limited(api: TestClient) -> TestClient:
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"max_uploads_per_day": 2})
    return api


def start_upload(client: TestClient) -> Any:
    return client.post("/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": 100})


def test_a_browser_past_its_daily_limit_is_refused_with_a_reason(limited: TestClient) -> None:
    assert [start_upload(limited).status_code for _ in range(2)] == [201, 201]
    res = start_upload(limited)
    assert (res.status_code, res.json()["error"]["code"]) == (429, "DAILY_LIMIT_REACHED")
    assert "2 uploads" in res.json()["error"]["message"]


def test_the_limit_is_per_browser(limited: TestClient, api_b: TestClient) -> None:
    for _ in range(2):
        assert start_upload(limited).status_code == 201
    assert start_upload(api_b).status_code == 201  # another browser has its own allowance


def test_uploads_older_than_a_day_do_not_count(limited: TestClient, db: Session) -> None:
    for _ in range(2):
        assert start_upload(limited).status_code == 201
    db.execute(update(AudioJob).where(AudioJob.owner_id == OWNER_A).values(created_at=datetime(2020, 1, 1, tzinfo=UTC)))
    db.commit()
    assert start_upload(limited).status_code == 201


def test_without_a_limit_nothing_is_counted(api: TestClient) -> None:
    assert get_settings().max_uploads_per_day == 0
    assert all(start_upload(api).status_code == 201 for _ in range(4))
