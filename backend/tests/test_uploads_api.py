import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fakes import FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import OWNER_A
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AudioJob, JobStatus
from app.db.session import get_sessionmaker
from app.services import uploads


def initiate(api: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {"filename": "Team Call.wav", "size_bytes": 5_000_000, **overrides}
    res = api.post("/api/uploads/initiate", json=body)
    assert res.status_code == 201, res.text
    return res.json()  # type: ignore[no-any-return]


def key_of(job_id: str, ext: str = ".wav") -> str:
    return f"uploads/{job_id}/audio{ext}"


def error(res: Any) -> tuple[int, str]:
    return res.status_code, res.json()["error"]["code"]


# --- initiate --------------------------------------------------------------------------------------------------


def test_initiate_creates_the_job_and_returns_a_signed_upload_target(
    api: TestClient, storage: FakeStorage, db: Session
) -> None:
    body = initiate(api)
    assert body["status"] == "UPLOADING"
    job_id = body["id"]
    assert body["upload"]["method"] == "PUT"
    assert body["upload"]["headers"] == {"Content-Type": "audio/wav"}  # chosen by the server, not the browser
    assert body["upload"]["url"].startswith(f"https://storage.test/uploads/{job_id}/audio.wav")
    assert storage.issued_uploads == [(key_of(job_id), "audio/wav", 5_000_000, 900)]

    row = db.scalars(select(AudioJob)).one()
    assert (str(row.id), row.object_key, row.mime_type, row.size_bytes) == (
        job_id,
        key_of(job_id),
        "audio/wav",
        5_000_000,
    )
    assert (row.original_filename, row.language_code, row.status) == (
        "Team Call.wav",
        "en-IN",
        JobStatus.UPLOADING,
    )


def test_initiate_never_uses_the_filename_in_the_storage_key(api: TestClient, db: Session) -> None:
    job_id = initiate(api, filename="../../etc/passwd.mp3")["id"]
    row = db.scalars(select(AudioJob)).one()
    assert row.original_filename == "passwd.mp3"
    assert row.object_key == key_of(job_id, ".mp3") and "passwd" not in row.object_key


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"filename": "notes.txt"}, (422, "UNSUPPORTED_FILE_TYPE")),
        ({"filename": "recording"}, (422, "UNSUPPORTED_FILE_TYPE")),
        ({"size_bytes": 0}, (422, "EMPTY_FILE")),
        ({"size_bytes": 2 * 1024**3 + 1}, (413, "FILE_TOO_LARGE")),
        ({"language_code": "gu-IN"}, (422, "UNSUPPORTED_LANGUAGE")),
        ({"filename": "  /  "}, (422, "INVALID_FILENAME")),
    ],
)
def test_invalid_uploads_are_refused_and_leave_no_trace(
    api: TestClient, storage: FakeStorage, db: Session, overrides: dict[str, Any], expected: tuple[int, str]
) -> None:
    res = api.post("/api/uploads/initiate", json={"filename": "a.mp3", "size_bytes": 10, **overrides})
    assert error(res) == expected
    assert res.json()["error"]["message"]  # always a human-readable message
    assert db.scalars(select(AudioJob)).all() == [] and storage.issued_uploads == []


def test_malformed_request_uses_the_same_error_shape(api: TestClient) -> None:
    res = api.post("/api/uploads/initiate", json={"filename": "a.mp3"})
    assert error(res) == (422, "INVALID_REQUEST")
    assert "size_bytes" in res.json()["error"]["message"]


def test_initiate_logs_an_event_but_never_the_signed_url(api: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        job_id = initiate(api)["id"]
    assert f"event=upload_created job_id={job_id}" in caplog.text
    assert "storage.test" not in caplog.text and "signature" not in caplog.text


# --- complete --------------------------------------------------------------------------------------------------


def test_complete_verifies_the_object_then_queues_the_job_for_the_worker(
    api: TestClient, storage: FakeStorage, queue: FakeQueue
) -> None:
    job_id = initiate(api)["id"]
    storage.put(key_of(job_id), 5_000_000)
    res = api.post(f"/api/uploads/{job_id}/complete")
    assert res.status_code == 200 and res.json()["status"] == "QUEUED"
    assert res.json()["progress_message"] == "Uploaded. Waiting for transcription…"
    assert api.get(f"/api/uploads/{job_id}").json()["status"] == "QUEUED"  # persisted, not just returned
    assert queue.enqueued == [(uuid.UUID(job_id), 0)]  # handed to the worker exactly once


def test_complete_is_idempotent_and_never_queues_twice(api: TestClient, storage: FakeStorage, queue: FakeQueue) -> None:
    job_id = initiate(api)["id"]
    storage.put(key_of(job_id), 5_000_000)
    first, second = (api.post(f"/api/uploads/{job_id}/complete") for _ in range(2))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert storage.head_calls == 1  # the duplicate call was answered from the database alone
    assert len(queue.enqueued) == 1  # and started no second processing run


def test_complete_before_the_bytes_arrive_is_retryable(api: TestClient, storage: FakeStorage) -> None:
    job_id = initiate(api)["id"]
    early = api.post(f"/api/uploads/{job_id}/complete")
    assert error(early) == (409, "UPLOAD_NOT_FOUND")
    assert api.get(f"/api/uploads/{job_id}").json()["status"] == "UPLOADING"  # not failed: it can still finish
    storage.put(key_of(job_id), 5_000_000)
    assert api.post(f"/api/uploads/{job_id}/complete").json()["status"] == "QUEUED"


def test_complete_with_the_wrong_size_fails_the_job_with_a_visible_reason(
    api: TestClient, storage: FakeStorage
) -> None:
    job_id = initiate(api)["id"]
    storage.put(key_of(job_id), 4_999_999)
    assert error(api.post(f"/api/uploads/{job_id}/complete")) == (409, "UPLOAD_SIZE_MISMATCH")
    job = api.get(f"/api/uploads/{job_id}").json()
    assert (job["status"], job["error_code"]) == ("FAILED", "UPLOAD_SIZE_MISMATCH")
    assert "upload it again" in job["error_message"]


def test_storage_outage_is_a_503_and_leaves_the_job_untouched(api: TestClient, storage: FakeStorage) -> None:
    job_id = initiate(api)["id"]
    storage.outage = True
    assert error(api.post(f"/api/uploads/{job_id}/complete")) == (503, "STORAGE_UNAVAILABLE")
    storage.outage = False
    assert api.get(f"/api/uploads/{job_id}").json()["status"] == "UPLOADING"
    storage.put(key_of(job_id), 5_000_000)
    assert api.post(f"/api/uploads/{job_id}/complete").json()["status"] == "QUEUED"


def test_complete_on_a_finished_or_failed_job_just_reports_it(api: TestClient, storage: FakeStorage) -> None:
    job_id = initiate(api)["id"]
    storage.put(key_of(job_id), 1)  # wrong size -> FAILED
    api.post(f"/api/uploads/{job_id}/complete")
    again = api.post(f"/api/uploads/{job_id}/complete")
    assert again.status_code == 200 and again.json()["status"] == "FAILED"


def test_concurrent_completions_produce_exactly_one_transition(
    db: Session, storage: FakeStorage, queue: FakeQueue
) -> None:
    """Two browser tabs (or a retrying client) completing at once must trigger the follow-up work once."""
    with get_sessionmaker()() as session:
        job, _url, _type = uploads.initiate_upload(
            session,
            storage,
            queue,
            _settings(),
            owner_id=OWNER_A,
            filename="a.wav",
            size_bytes=10,
            language_code="en-IN",
        )
        job_id = job.id
    storage.put(f"uploads/{job_id}/audio.wav", 10)
    storage.head_barrier = threading.Barrier(6)  # all six have seen UPLOADING before any of them completes it

    def attempt(_: int) -> bool:
        with get_sessionmaker()() as s:
            return uploads.complete_upload(s, storage, queue, job_id)[1]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert storage.head_calls == 6  # proof the race really happened: none of them took the early "already done" exit
    assert results.count(True) == 1
    assert len(queue.enqueued) == 1  # six racing calls, one processing run


def _settings() -> Any:
    from app.core.config import get_settings

    return get_settings()


# --- read ------------------------------------------------------------------------------------------------------


def test_unknown_and_malformed_ids(api: TestClient) -> None:
    assert error(api.get(f"/api/uploads/{uuid.uuid4()}")) == (404, "JOB_NOT_FOUND")
    assert error(api.post(f"/api/uploads/{uuid.uuid4()}/complete")) == (404, "JOB_NOT_FOUND")
    assert error(api.get("/api/uploads/not-a-uuid")) == (422, "INVALID_REQUEST")


def test_detail_exposes_state_but_no_storage_or_provider_internals(api: TestClient) -> None:
    job = api.get(f"/api/uploads/{initiate(api)['id']}").json()
    assert job["status"] == "UPLOADING" and job["transcript"] is None and job["summary"] is None
    assert job["original_filename"] == "Team Call.wav" and job["language_code"] == "en-IN"
    assert not {"object_key", "gnani_job_id", "gnani_status"} & job.keys()


def test_history_lists_newest_first_and_survives_a_refresh(api: TestClient) -> None:
    ids = [initiate(api, filename=f"call{n}.mp3")["id"] for n in range(3)]
    listed = api.get("/api/uploads").json()  # the same call a refreshed page makes: state comes from Postgres
    assert [item["id"] for item in listed] == ids[::-1]
    assert "transcript" not in listed[0] and "summary" not in listed[0]  # list rows stay small
    assert [item["id"] for item in api.get("/api/uploads?limit=2").json()] == ids[::-1][:2]


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_list_limit_is_bounded(api: TestClient, limit: int) -> None:
    assert error(api.get(f"/api/uploads?limit={limit}")) == (422, "INVALID_REQUEST")
