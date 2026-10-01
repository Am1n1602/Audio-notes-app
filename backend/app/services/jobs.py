"""Database operations on audio jobs. The only place that changes a job's status."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, defer

from app.core.errors import AppError
from app.core.failures import Failure
from app.db.models import AudioJob, JobStatus

# The job state machine: nothing moves backwards, and any stage can fail. The single backwards edge is the
# user-requested retry (FAILED -> QUEUED), which starts a brand-new attempt with its own claim.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.UPLOADING: frozenset({JobStatus.UPLOADED, JobStatus.FAILED}),
    JobStatus.UPLOADED: frozenset({JobStatus.QUEUED, JobStatus.FAILED}),
    JobStatus.QUEUED: frozenset({JobStatus.TRANSCRIBING, JobStatus.FAILED}),
    JobStatus.TRANSCRIBING: frozenset({JobStatus.SUMMARIZING, JobStatus.FAILED}),
    JobStatus.SUMMARIZING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    # Retry re-enters where it makes sense: QUEUED after a transcription-stage failure, SUMMARIZING after a
    # summary-stage failure (the transcript is already saved, so nothing is re-transcribed).
    JobStatus.FAILED: frozenset({JobStatus.QUEUED, JobStatus.SUMMARIZING}),
}

# Statuses a job can still fail from (everything between "verified upload" and "finished").
ACTIVE_STATUSES = (JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING)

# A step may run up to this long before the job's recorded due time (clock differences between the API, worker and
# database hosts, and the small gap between recording the time and the queue delivering the message).
STEP_EARLY_TOLERANCE = timedelta(seconds=5)

# What the UI shows for each stage. Persisted at every transition so a refreshed page reads it from Postgres.
# Honest stage text only: never a made-up percentage.
PROGRESS_MESSAGES: dict[JobStatus, str] = {
    JobStatus.QUEUED: "Uploaded. Waiting for transcription…",
    JobStatus.TRANSCRIBING: "Transcribing audio…",
    JobStatus.SUMMARIZING: "Generating summary…",
}
# While Gnani does not answer, the job is not "being transcribed" as far as anyone can tell, and every retry touches the
# job (so "last activity" stays fresh): without this the page would say "Transcribing audio…" for up to two hours.
WAITING_FOR_GNANI_MESSAGE = "Waiting for the transcription service to respond…"


def create_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    owner_id: str,
    original_filename: str,
    object_key: str,
    mime_type: str,
    size_bytes: int,
    language_code: str,
) -> AudioJob:
    job = AudioJob(
        id=job_id,
        owner_id=owner_id,
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


def list_jobs(db: Session, limit: int, owner_id: str) -> list[AudioJob]:
    # id as a tie-breaker keeps the order stable when two rows share a timestamp. The history list never shows the
    # big columns (a 4-hour transcript is ~200 KB), so they are not loaded: `defer` leaves them out of the SELECT.
    statement = (
        select(AudioJob)
        .where(AudioJob.owner_id == owner_id)  # one browser's history only
        .options(defer(AudioJob.transcript), defer(AudioJob.summary), defer(AudioJob.summary_partials))
        .order_by(AudioJob.created_at.desc(), AudioJob.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


def count_started_since(db: Session, owner_id: str, within: timedelta) -> int:
    """How many uploads this browser has started in the last `within` (the database's clock, like every other time
    comparison here). Counts every row, finished or not: an upload that never completes still left a file in storage."""
    statement = select(func.count()).where(AudioJob.owner_id == owner_id, AudioJob.created_at > func.now() - within)
    return db.scalar(statement) or 0


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
    values.setdefault("progress_message", PROGRESS_MESSAGES.get(to_status))  # None clears it (FAILED, COMPLETED, ...)
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


def set_progress_message(db: Session, job_id: uuid.UUID, status: JobStatus, message: str) -> None:
    """Change what the page says is happening, but only while the job is still in `status`, and only if it differs (so a
    repeated call touches nothing and "last activity" keeps meaning real activity)."""
    db.execute(
        update(AudioJob)
        .where(AudioJob.id == job_id, AudioJob.status == status, AudioJob.progress_message.is_distinct_from(message))
        .values(progress_message=message)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def set_gnani_job_id(db: Session, job_id: uuid.UUID, gnani_job_id: str) -> bool:
    """Record the Gnani job id the moment Create returns. Guarded so it can only ever be set once, on a claimed job."""
    statement = (
        update(AudioJob)
        .where(AudioJob.id == job_id, AudioJob.status == JobStatus.TRANSCRIBING, AudioJob.gnani_job_id.is_(None))
        .values(gnani_job_id=gnani_job_id, gnani_status="CREATED")
        .returning(AudioJob.id)
        .execution_options(synchronize_session=False)
    )
    saved = db.execute(statement).first() is not None
    db.commit()
    return saved


def record_provider_status(db: Session, job_id: uuid.UUID, gnani_status: str) -> None:
    """Keep Gnani's raw status next to ours, for debugging. Only writes when it actually changed."""
    db.execute(
        update(AudioJob)
        .where(
            AudioJob.id == job_id,
            AudioJob.status == JobStatus.TRANSCRIBING,
            AudioJob.gnani_status.is_distinct_from(gnani_status),
        )
        .values(gnani_status=gnani_status)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def fail_job(db: Session, job_id: uuid.UUID, failure: Failure) -> bool:
    """Mark a job FAILED from any active stage. False if it had already finished or failed."""
    statement = (
        update(AudioJob)
        .where(AudioJob.id == job_id, AudioJob.status.in_(ACTIVE_STATUSES))
        .values(status=JobStatus.FAILED, error_code=failure.code, error_message=failure.message, progress_message=None)
        .returning(AudioJob.id)
        .execution_options(synchronize_session=False)
    )
    failed = db.execute(statement).first() is not None
    db.commit()
    return failed


def requeue_failed(db: Session, job_id: uuid.UUID, *, resume_summary: bool = False) -> bool:
    """A user-requested retry.

    Default: FAILED -> QUEUED, clearing everything the failed attempt left behind, so the next attempt starts clean
    and wins its own atomic claim.
    resume_summary=True: FAILED -> SUMMARIZING for a failure that happened after the transcript was saved. The
    transcript (and any finished part-summaries) are KEPT, so a retry never pays to transcribe again.
    """
    if resume_summary:
        return transition(
            db, job_id, JobStatus.FAILED, JobStatus.SUMMARIZING, error_code=None, error_message=None, next_step_at=None
        )
    return transition(
        db,
        job_id,
        JobStatus.FAILED,
        JobStatus.QUEUED,
        error_code=None,
        error_message=None,
        gnani_job_id=None,
        gnani_status=None,
        submit_started_at=None,
        transcript=None,
        duration_seconds=None,
        summary=None,
        summary_partials=None,
        next_step_at=None,
    )


def save_summary_partials(db: Session, job_id: uuid.UUID, expected_count: int, partials: list[dict[str, Any]]) -> bool:
    """Replace the saved part-summaries, but only if exactly `expected_count` are saved right now. Appending a part
    and folding several into one both change the count, so a duplicate step (or a crash and retry) finds the count
    no longer matches and saves nothing: work is never stored twice. Returns whether THIS call saved it."""
    saved_count = func.coalesce(func.jsonb_array_length(AudioJob.summary_partials), 0)
    statement = (
        update(AudioJob)
        .where(AudioJob.id == job_id, AudioJob.status == JobStatus.SUMMARIZING, saved_count == expected_count)
        .values(summary_partials=literal(partials, JSONB))
        .returning(AudioJob.id)
        .execution_options(synchronize_session=False)
    )
    saved = db.execute(statement).first() is not None
    db.commit()
    return saved


# --- one chain of steps per job -------------------------------------------------------------------------------


def is_step_due(db: Session, job_id: uuid.UUID) -> bool:
    """False when this step arrived well before the job's recorded due time: it is a duplicate of a step chain that
    is already running (a second worker start, a replica, a redelivered message), so it must end here instead of
    rescheduling itself. Otherwise two chains would each poll Gnani and each spend an LLM call per interval for the
    rest of the job. Uses the database clock on both sides, so it needs no agreement between hosts."""
    statement = select(func.count()).where(
        AudioJob.id == job_id,
        (AudioJob.next_step_at.is_(None)) | (AudioJob.next_step_at <= func.now() + STEP_EARLY_TOLERANCE),
    )
    return bool(db.scalar(statement))


def set_next_step(db: Session, job_id: uuid.UUID, seconds: int) -> None:
    """Record that the next step is due `seconds` from now. Called just before that step is enqueued."""
    db.execute(
        update(AudioJob)
        .where(AudioJob.id == job_id)
        .values(next_step_at=func.now() + timedelta(seconds=seconds))
        .execution_options(synchronize_session=False)
    )
    db.commit()


def claim_stalled(db: Session, job_ids: list[uuid.UUID], after_seconds: int) -> list[uuid.UUID]:
    """Of these jobs, the ones that really have had no step for `after_seconds` (judged by the database clock) and are
    still in progress, claimed in the same statement: it also clears the due time and bumps updated_at, so a second
    caller, or the same page asking again a moment later, finds nothing stalled and revives nothing twice."""
    if not job_ids:
        return []
    statement = (
        update(AudioJob)
        .where(
            AudioJob.id.in_(job_ids),
            AudioJob.status.in_(ACTIVE_STATUSES),
            AudioJob.updated_at < func.now() - timedelta(seconds=after_seconds),
        )
        .values(next_step_at=None)
        .returning(AudioJob.id)
        .execution_options(synchronize_session=False)
    )
    claimed = list(db.scalars(statement))
    db.commit()
    return claimed


def clear_next_step(db: Session, job_id: uuid.UUID) -> None:
    """A fresh chain starts (or the last enqueue failed): a step is due right now."""
    db.execute(
        update(AudioJob)
        .where(AudioJob.id == job_id)
        .values(next_step_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
