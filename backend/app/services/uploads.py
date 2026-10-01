"""Upload lifecycle: initiate (validate, sign a URL), complete (verify, hand to the worker), retry."""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings
from app.core.errors import AppError
from app.core.failures import Failure
from app.core.logging import log_event
from app.db.models import AudioJob, JobStatus
from app.providers.queue import JobQueue, QueueError
from app.providers.storage import ObjectStorage, StorageError
from app.services import jobs
from app.services.upload_rules import object_key, validate_upload

logger = logging.getLogger(__name__)


def initiate_upload(
    db: Session,
    storage: ObjectStorage,
    settings: Settings,
    *,
    owner_id: str,
    filename: str,
    size_bytes: int,
    language_code: str,
) -> tuple[AudioJob, str, str]:
    """Returns (job, signed upload URL, the Content-Type the browser must send)."""
    upload = validate_upload(filename, size_bytes, language_code, settings.max_upload_bytes)
    limit = settings.max_uploads_per_day
    if limit and jobs.count_started_since(db, owner_id, timedelta(hours=24)) >= limit:
        # The demo is public and has no sign-in, so this is what stops one browser spending the provider quotas.
        log_event(logger, "upload_limit_reached", limit=limit)
        raise AppError(
            429,
            "DAILY_LIMIT_REACHED",
            f"This demo allows {limit} uploads per browser in 24 hours and you have used them. Please try again later.",
        )
    job_id = uuid.uuid4()
    key = object_key(job_id, upload.extension)
    # Sign before inserting: signing is pure computation, so nothing external is touched if the insert then fails.
    url = storage.create_upload_url(key, upload.mime_type, upload.size_bytes, settings.upload_url_expires_seconds)
    job = jobs.create_job(
        db,
        job_id=job_id,
        owner_id=owner_id,
        original_filename=upload.original_filename,
        object_key=key,
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        language_code=upload.language_code,
    )
    log_event(logger, "upload_created", job_id=job.id, size_bytes=job.size_bytes, mime_type=job.mime_type)
    return job, url, upload.mime_type


def complete_upload(db: Session, storage: ObjectStorage, queue: JobQueue, job_id: uuid.UUID) -> tuple[AudioJob, bool]:
    """Confirm the browser's upload really landed in storage, then hand the job to the worker.
    Returns (job, this_call_made_the_transition).

    Safe to call twice, or concurrently: only the call that wins the guarded UPLOADING -> UPLOADED transition
    enqueues the processing task, which is what stops a duplicate /complete from starting two processing runs.
    """
    job = jobs.get_job(db, job_id)
    if job.status is not JobStatus.UPLOADING:
        return job, False  # duplicate or late call: report the current state, change nothing

    try:
        stored = storage.head(job.object_key)
    except StorageError as exc:
        log_event(logger, "storage_error", job_id=job.id, error=exc)
        raise AppError(
            503, "STORAGE_UNAVAILABLE", "File storage is not reachable right now. Please try again."
        ) from exc

    if stored is None:
        # Not a failure of the job: the bytes may still be arriving, or the browser upload failed. The job stays
        # UPLOADING so the client can retry the call (or upload again).
        raise AppError(
            409,
            "UPLOAD_NOT_FOUND",
            "The file has not arrived in storage. If the upload just finished, try again in a moment; "
            "otherwise upload the file again.",
        )

    if stored.size_bytes != job.size_bytes:
        # Cannot happen through a valid signed URL (Content-Length is signed), so treat it as a real failure.
        message = "The uploaded file does not match the size that was declared. Please upload it again."
        jobs.transition(
            db,
            job.id,
            JobStatus.UPLOADING,
            JobStatus.FAILED,
            error_code="UPLOAD_SIZE_MISMATCH",
            error_message=message,
        )
        log_event(logger, "job_failed", job_id=job.id, error_code="UPLOAD_SIZE_MISMATCH")
        raise AppError(409, "UPLOAD_SIZE_MISMATCH", message)

    moved = jobs.transition(db, job.id, JobStatus.UPLOADING, JobStatus.UPLOADED)
    if moved:
        log_event(logger, "upload_completed", job_id=job.id, size_bytes=job.size_bytes)
        _enqueue_or_fail(db, queue, job.id)
        # Enqueue first, mark QUEUED second: if we crash in between the worker still finds an UPLOADED job and
        # advances it itself. False here just means the worker got there first.
        jobs.transition(db, job.id, JobStatus.UPLOADED, JobStatus.QUEUED)
    db.refresh(job)  # the guarded UPDATEs bypassed the ORM, so reload what is now in the row
    return job, moved


def retry_upload(db: Session, queue: JobQueue, job_id: uuid.UUID) -> AudioJob:
    """User pressed Retry on a failed job. Starts a brand-new attempt (it will win its own claim) and re-enqueues.

    Only failures where trying again can plausibly help are retryable (failures.RETRYABLE_CODES). A second Retry
    while the first is running changes nothing: only the call that wins FAILED -> QUEUED enqueues.
    """
    job = jobs.get_job(db, job_id)
    if job.status is JobStatus.FAILED and job.error_code not in failures.RETRYABLE_CODES:
        raise AppError(409, "NOT_RETRYABLE", "This upload cannot be retried. Please upload the file again.")
    if job.status not in (JobStatus.FAILED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING):
        raise AppError(409, "NOT_RETRYABLE", "Only a failed upload can be retried.")
    # A transcript is only ever saved when transcription finished (a full retry clears it), so a failed job that has
    # one failed at or after the summary, whatever its error code says (a crash records INTERNAL_ERROR, a dead queue
    # overwrites the code with QUEUE_UNAVAILABLE). It resumes at the summary: nothing is transcribed twice.
    resume_summary = bool(job.transcript)
    if job.status is JobStatus.FAILED and jobs.requeue_failed(db, job.id, resume_summary=resume_summary):
        log_event(
            logger, "job_retried", job_id=job.id, previous_error_code=job.error_code, resumes_at_summary=resume_summary
        )
        _enqueue_or_fail(db, queue, job.id)
    db.refresh(job)
    return job


def audio_url(storage: ObjectStorage, settings: Settings, job: AudioJob) -> str:
    """A signed link for playing the recording back. Only once the upload was verified: before that there may be no
    object, or (after a size mismatch) the wrong one."""
    if job.status is JobStatus.UPLOADING or job.error_code == failures.UPLOAD_SIZE_MISMATCH:
        raise AppError(409, "AUDIO_NOT_AVAILABLE", "The recording has not finished uploading.")
    return storage.create_download_url(job.object_key, settings.audio_url_expires_seconds)


def _enqueue_or_fail(db: Session, queue: JobQueue, job_id: uuid.UUID) -> None:
    try:
        queue.enqueue_process_job(job_id)
    except QueueError as exc:
        failure = Failure(
            failures.QUEUE_UNAVAILABLE,
            "Your file is uploaded, but we could not start processing it. Please press Retry.",
        )
        jobs.fail_job(db, job_id, failure)
        log_event(logger, "job_failed", job_id=job_id, error_code=failure.code, cause=type(exc).__name__)
        raise AppError(503, failure.code, failure.message) from exc
