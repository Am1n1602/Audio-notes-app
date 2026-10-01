"""What changes when a recording is long: size limits at the boundary, and a transcript big enough to matter."""

from typing import Any

from fakes import FakeStorage
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import JobStatus

FOUR_HOUR_TRANSCRIPT = "the quarterly review covered the billing team and the data platform budget " * 3000  # ~220 KB


def initiate(api: TestClient, size_bytes: int) -> Any:
    return api.post("/api/uploads/initiate", json={"filename": "all-day.wav", "size_bytes": size_bytes})


def test_a_recording_exactly_at_the_size_limit_is_accepted_and_signed_for_that_size(
    api: TestClient, storage: FakeStorage
) -> None:
    limit = get_settings().max_upload_bytes
    res = initiate(api, limit)
    assert res.status_code == 201
    assert storage.issued_uploads[-1][2] == limit  # the link is signed for exactly that many bytes, however many
    assert initiate(api, limit + 1).status_code == 413
    assert len(storage.issued_uploads) == 1  # and one over is refused before anything is signed


def test_a_four_hour_transcript_comes_back_whole_and_compressed(api: TestClient, db: Session) -> None:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(text("UPDATE audio_jobs SET transcript = :t WHERE id = :i"), {"t": FOUR_HOUR_TRANSCRIPT, "i": job_id})
    db.commit()

    plain = api.get(f"/api/uploads/{job_id}", headers={"Accept-Encoding": "identity"})
    packed = api.get(f"/api/uploads/{job_id}", headers={"Accept-Encoding": "gzip"})

    assert plain.status_code == packed.status_code == 200
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.json()["transcript"] == FOUR_HOUR_TRANSCRIPT  # nothing lost to compression or truncation
    assert len(plain.content) > 200_000


def test_small_answers_are_not_compressed(api: TestClient) -> None:
    res = api.get("/api/config", headers={"Accept-Encoding": "gzip"})
    assert res.status_code == 200 and "content-encoding" not in res.headers
