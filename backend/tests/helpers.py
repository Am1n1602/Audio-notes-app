import uuid

from sqlalchemy.orm import Session

from app.api.owner import owner_id_for
from app.db.models import JobStatus
from app.services import jobs

# Two different browsers. Every test API call is made as CLIENT_A unless it says otherwise, and the jobs the helpers
# create belong to CLIENT_A, so a test can create a job in the database and then read it through the API.
CLIENT_A = "0b7a6f0e-5d51-4c3e-9a3b-6f1f0c0a1111"
CLIENT_B = "9c2d4e1a-7b83-4f60-8d52-2e9a7b5c2222"
OWNER_A = owner_id_for(CLIENT_A)
OWNER_B = owner_id_for(CLIENT_B)
HEADERS_A = {"X-Client-Id": CLIENT_A}
HEADERS_B = {"X-Client-Id": CLIENT_B}

_PATH = [JobStatus.UPLOADING, JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING]


def job_in_status(db: Session, status: JobStatus = JobStatus.QUEUED) -> uuid.UUID:
    """A job walked through the real state machine up to `status`."""
    job_id = uuid.uuid4()
    jobs.create_job(
        db,
        job_id=job_id,
        owner_id=OWNER_A,
        original_filename="call.wav",
        object_key=f"uploads/{job_id}/audio.wav",
        mime_type="audio/wav",
        size_bytes=100,
        language_code="hi-IN,en-IN",
    )
    for src, dst in zip(_PATH, _PATH[1:], strict=False):
        if src is status:
            break
        assert jobs.transition(db, job_id, src, dst)
    return job_id


def assert_test_database(name: str) -> None:
    """The suite DROPs and recreates its database. Refuse anything that is not clearly a throwaway one, so a
    misconfigured DATABASE_URL can never point it at real data."""
    if not name.endswith("_test"):
        raise RuntimeError(f"refusing to drop database {name!r}: the test database name must end in '_test'")
