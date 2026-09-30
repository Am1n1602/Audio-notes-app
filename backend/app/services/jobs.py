"""Database operations on audio jobs. The only place that changes a job's status."""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import AudioJob, JobStatus

# The job state machine: nothing moves backwards, and any stage can fail.
# FAILED is terminal for now; "retry" (Phase 3) will add an explicit FAILED -> UPLOADED edge.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.UPLOADING: frozenset({JobStatus.UPLOADED, JobStatus.FAILED}),
    JobStatus.UPLOADED: frozenset({JobStatus.QUEUED, JobStatus.FAILED}),
    JobStatus.QUEUED: frozenset({JobStatus.TRANSCRIBING, JobStatus.FAILED}),
    JobStatus.TRANSCRIBING: frozenset({JobStatus.SUMMARIZING, JobStatus.FAILED}),
    JobStatus.SUMMARIZING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


def create_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    original_filename: str,
    object_key: str,
    mime_type: str,
    size_bytes: int,
    language_code: str,
) -> AudioJob:
    job = AudioJob(
        id=job_id,
        original_filename=original_filename,
        object_key=object_key,
        mime_type=mime_type,
        size_bytes=size_bytes,
        language_code=language_code,
        status=JobStatus.UPLOADING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: uuid.UUID) -> AudioJob:
    job = db.get(AudioJob, job_id)
    if job is None:
        raise AppError(404, "JOB_NOT_FOUND", "We could not find that upload.")
    return job


def list_jobs(db: Session, limit: int) -> list[AudioJob]:
    # id as a tie-breaker keeps the order stable when two rows share a timestamp
    return list(db.scalars(select(AudioJob).order_by(AudioJob.created_at.desc(), AudioJob.id.desc()).limit(limit)))


def transition(db: Session, job_id: uuid.UUID, from_status: JobStatus, to_status: JobStatus, **fields: Any) -> bool:
    """Move a job from one status to the next, atomically. True if THIS call made the move.

    The WHERE clause is the guard: UPDATE ... WHERE id = :id AND status = :from_status. If a duplicate request or
    another worker already moved the job, zero rows match and we return False. Under Postgres' default isolation
    level a concurrent second UPDATE waits for the first to commit, then re-checks the WHERE against the new row,
    so exactly one caller wins. Whoever gets True owns the side effect (enqueueing work, etc.); everyone else
    treats it as "already done".
    """
    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise ValueError(f"illegal job transition {from_status} -> {to_status}")
    values: dict[str, Any] = {"status": to_status, **fields}
    if to_status is JobStatus.COMPLETED:
        values["completed_at"] = func.now()
    statement = (
        update(AudioJob)
        .where(AudioJob.id == job_id, AudioJob.status == from_status)
        .values(**values)
        .returning(AudioJob.id)
        .execution_options(synchronize_session=False)
    )
    moved = db.execute(statement).first() is not None
    db.commit()
    return moved
