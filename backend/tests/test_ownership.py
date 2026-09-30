"""Per-browser history: each browser sees only the uploads it made, and a job id alone is not enough to read one."""

import uuid
from typing import Any

import pytest
from fakes import FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import CLIENT_A, HEADERS_A, OWNER_A, OWNER_B, job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.owner import owner_id_for
from app.db.models import JobStatus
from app.main import app
from app.services import jobs

NOT_FOUND = {"error": {"code": "JOB_NOT_FOUND", "message": "We could not find that upload."}}


def body(res: Any) -> dict[str, Any]:
    return res.json()  # type: ignore[no-any-return]


def ids(res: Any) -> list[str]:
    """The job ids in a list response, in order."""
    return [item["id"] for item in res.json()]


def upload(client: TestClient, name: str = "a.wav") -> str:
    res = client.post("/api/uploads/initiate", json={"filename": name, "size_bytes": 100})
    assert res.status_code == 201
    return str(body(res)["id"])


# --- who is asking --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/uploads/initiate"),
        ("get", "/api/uploads"),
        ("get", f"/api/uploads/{uuid.uuid4()}"),
        ("post", f"/api/uploads/{uuid.uuid4()}/complete"),
        ("post", f"/api/uploads/{uuid.uuid4()}/retry"),
    ],
)
def test_every_upload_endpoint_needs_a_client_id(api: TestClient, method: str, path: str) -> None:
    anonymous = TestClient(app)  # no X-Client-Id header
    res = getattr(anonymous, method)(
        path, **({"json": {"filename": "a.wav", "size_bytes": 1}} if "initiate" in path else {})
    )
    assert (res.status_code, body(res)["error"]["code"]) == (401, "CLIENT_ID_REQUIRED")


@pytest.mark.parametrize(
    "client_id",
    [
        "not-a-uuid",
        "12345",
        "0b7a6f0e-5d51-1c3e-9a3b-6f1f0c0a1111",
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    ],  # last two: not v4
)
def test_a_client_id_must_be_a_random_uuid(api: TestClient, client_id: str) -> None:
    res = TestClient(app, headers={"X-Client-Id": client_id}).get("/api/uploads")
    assert (res.status_code, body(res)["error"]["code"]) == (401, "CLIENT_ID_INVALID")


@pytest.mark.parametrize(
    "variant", [CLIENT_A.upper(), "{" + CLIENT_A + "}", "urn:uuid:" + CLIENT_A, CLIENT_A.replace("-", "")]
)
def test_spellings_of_the_same_id_are_the_same_browser(api: TestClient, variant: str) -> None:
    job_id = upload(api)
    res = TestClient(app, headers={"X-Client-Id": variant}).get(f"/api/uploads/{job_id}")
    assert res.status_code == 200 and body(res)["id"] == job_id


def test_health_needs_no_client_id(api: TestClient) -> None:
    assert TestClient(app).get("/api/health").status_code in (200, 503)  # answered, not a 401


# --- a browser sees only its own uploads ------------------------------------------------------------------------------


def test_each_browser_lists_only_its_own_uploads_newest_first(api: TestClient, api_b: TestClient) -> None:
    first, second = upload(api, "one.wav"), upload(api, "two.wav")
    theirs = upload(api_b, "theirs.wav")
    assert ids(api.get("/api/uploads")) == [second, first]
    assert ids(api_b.get("/api/uploads")) == [theirs]


def test_the_limit_applies_within_one_browsers_history(api: TestClient, api_b: TestClient) -> None:
    for n in range(3):
        upload(api, f"{n}.wav")
    upload(api_b)
    assert len(body(api.get("/api/uploads?limit=2"))) == 2


def test_another_browser_cannot_read_complete_or_retry_a_job_even_with_its_id(
    api: TestClient, api_b: TestClient, storage: FakeStorage, queue: FakeQueue, db: Session
) -> None:
    job_id = upload(api)
    storage.put(f"uploads/{job_id}/audio.wav", 100)  # the file really is there: only ownership stops B

    for res in (
        api_b.get(f"/api/uploads/{job_id}"),
        api_b.post(f"/api/uploads/{job_id}/complete"),
        api_b.post(f"/api/uploads/{job_id}/retry"),
    ):
        assert (res.status_code, body(res)) == (404, NOT_FOUND)
    assert queue.enqueued == []  # B's /complete started nothing
    db.expire_all()
    assert jobs.get_job(db, uuid.UUID(job_id)).status is JobStatus.UPLOADING  # and changed nothing
    assert api.post(f"/api/uploads/{job_id}/complete").status_code == 200  # the owner still can


def test_someone_elses_job_looks_exactly_like_one_that_does_not_exist(api: TestClient, api_b: TestClient) -> None:
    job_id = upload(api)
    real_but_not_yours = api_b.get(f"/api/uploads/{job_id}")
    made_up = api_b.get(f"/api/uploads/{uuid.uuid4()}")
    assert (real_but_not_yours.status_code, real_but_not_yours.json()) == (made_up.status_code, made_up.json())


def test_a_failed_job_can_only_be_retried_by_its_owner(
    api: TestClient, api_b: TestClient, db: Session, queue: FakeQueue
) -> None:
    from app.core import failures

    job_id = job_in_status(db, JobStatus.UPLOADED)  # created for browser A by the helper
    jobs.fail_job(db, job_id, failures.Failure(failures.TRANSCRIPTION_UNAVAILABLE, "busy"))
    assert api_b.post(f"/api/uploads/{job_id}/retry").status_code == 404 and queue.enqueued == []
    assert api.post(f"/api/uploads/{job_id}/retry").status_code == 200 and len(queue.enqueued) == 1


# --- what is stored ---------------------------------------------------------------------------------------------------


def test_the_job_stores_a_hash_of_the_client_id_never_the_id(api: TestClient, db: Session) -> None:
    job_id = upload(api)
    stored: str = db.execute(text("SELECT owner_id FROM audio_jobs WHERE id = :i"), {"i": job_id}).scalar_one()
    assert stored == OWNER_A == owner_id_for(CLIENT_A) and len(stored) == 64
    assert CLIENT_A not in stored and stored != OWNER_B  # a database leak does not hand out working client ids


def test_a_job_with_no_owner_is_shown_to_nobody(api: TestClient, api_b: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADED)
    db.execute(text("UPDATE audio_jobs SET owner_id = NULL WHERE id = :i"), {"i": job_id})
    db.commit()
    for client in (api, api_b):
        assert client.get(f"/api/uploads/{job_id}").status_code == 404
        assert client.get("/api/uploads").json() == []


def test_the_owner_is_never_part_of_the_response(api: TestClient) -> None:
    job_id = upload(api)
    assert (
        "owner" not in api.get(f"/api/uploads/{job_id}").text.lower()
        and "owner" not in api.get("/api/uploads").text.lower()
    )
    assert (
        OWNER_A not in api.get(f"/api/uploads/{job_id}").text
        and HEADERS_A["X-Client-Id"] not in api.get("/api/uploads").text
    )
