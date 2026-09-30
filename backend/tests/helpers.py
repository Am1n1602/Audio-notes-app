import uuid

from sqlalchemy.orm import Session

from app.db.models import JobStatus
from app.services import jobs

_PATH = [JobStatus.UPLOADING, JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING]


def job_in_status(db: Session, status: JobStatus = JobStatus.QUEUED) -> uuid.UUID:
    """A job walked through the real state machine up to `status`."""
    job_id = uuid.uuid4()
    jobs.create_job(
        db,
        job_id=job_id,
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
