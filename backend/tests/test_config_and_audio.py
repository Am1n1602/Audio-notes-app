"""The two endpoints the page needs beyond upload, list and detail: the real limits, and a link for playback."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import get_settings
from app.db.models import JobStatus
from app.main import app
from app.services import jobs
from app.services.upload_rules import AUDIO_TYPES, LANGUAGE_NAMES, SUPPORTED_LANGUAGES


def body(res: Any) -> dict[str, Any]:
    return res.json()  # type: ignore[no-any-return]


# --- GET /api/config ----------------------------------------------------------------------------------------------


def test_config_reports_the_limits_the_backend_enforces(api: TestClient) -> None:
    config = body(TestClient(app).get("/api/config"))  # public: no client id
    settings = get_settings()
    assert config["max_upload_bytes"] == settings.max_upload_bytes
    assert config["upload_url_expires_seconds"] == settings.upload_url_expires_seconds
    assert config["audio_extensions"] == list(AUDIO_TYPES) and ".wav" in config["audio_extensions"]
    assert {lang["code"] for lang in config["languages"]} == SUPPORTED_LANGUAGES
    assert config["max_languages"] == 3 and config["default_language_code"] == "en-IN"


def test_every_language_has_a_readable_name() -> None:
    assert set(LANGUAGE_NAMES) == SUPPORTED_LANGUAGES
    assert all(name.strip() and name != code for code, name in LANGUAGE_NAMES.items())


def test_what_config_advertises_is_what_initiate_accepts(api: TestClient) -> None:
    """The page builds its file filter and language list from /config, so every advertised value must be accepted."""
    config = body(api.get("/api/config"))
    for extension in config["audio_extensions"]:
        res = api.post("/api/uploads/initiate", json={"filename": f"clip{extension}", "size_bytes": 10})
        assert res.status_code == 201, extension
    for language in config["languages"]:
        res = api.post(
            "/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": 10, "language_code": language["code"]}
        )
        assert res.status_code == 201, language["code"]
    too_big = api.post(
        "/api/uploads/initiate", json={"filename": "a.wav", "size_bytes": config["max_upload_bytes"] + 1}
    )
    assert too_big.status_code == 413  # the advertised size limit is the real one


# --- GET /api/uploads/{id}/audio ------------------------------------------------------------------------------------


def test_a_verified_recording_gets_a_short_lived_signed_link(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)
    res = api.get(f"/api/uploads/{job_id}/audio")
    assert res.status_code == 200
    link = body(res)
    assert link["url"].startswith(f"https://storage.test/uploads/{job_id}/audio.wav")
    assert f"expires={get_settings().audio_url_expires_seconds}" in link["url"]
    assert link["expires_in_seconds"] == get_settings().audio_url_expires_seconds
    assert res.headers["cache-control"] == "no-store"  # a credential while valid: never cached


@pytest.mark.parametrize("status", [JobStatus.UPLOADED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING])
def test_the_link_is_available_at_every_stage_after_the_upload(api: TestClient, db: Session, status: JobStatus) -> None:
    assert api.get(f"/api/uploads/{job_in_status(db, status)}/audio").status_code == 200


def test_a_failed_job_whose_file_was_verified_can_still_be_played(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    jobs.fail_job(db, job_id, failures.Failure(failures.SUMMARY_UNAVAILABLE, "busy"))
    assert api.get(f"/api/uploads/{job_id}/audio").status_code == 200


def test_there_is_no_link_until_the_upload_was_verified(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADING)
    res = api.get(f"/api/uploads/{job_id}/audio")
    assert (res.status_code, body(res)["error"]["code"]) == (409, "AUDIO_NOT_AVAILABLE")


def test_there_is_no_link_for_an_upload_whose_size_did_not_match(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADING)
    db.execute(
        text("UPDATE audio_jobs SET status = 'FAILED', error_code = :c WHERE id = :i"),
        {"c": failures.UPLOAD_SIZE_MISMATCH, "i": job_id},
    )
    db.commit()
    assert api.get(f"/api/uploads/{job_id}/audio").status_code == 409  # the stored object is not the one declared


def test_the_link_belongs_to_the_owner_only(api: TestClient, api_b: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)  # created for browser A
    assert api_b.get(f"/api/uploads/{job_id}/audio").status_code == 404  # same as a missing job
    assert api_b.get(f"/api/uploads/{uuid.uuid4()}/audio").json() == api_b.get(f"/api/uploads/{job_id}/audio").json()


def test_the_link_needs_a_client_id(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.QUEUED)
    res = TestClient(app).get(f"/api/uploads/{job_id}/audio")
    assert (res.status_code, body(res)["error"]["code"]) == (401, "CLIENT_ID_REQUIRED")
