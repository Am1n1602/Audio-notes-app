"""Upload lifecycle: initiate (validate, create the job, sign a URL) and complete (verify the object, mark uploaded)."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import log_event
from app.db.models import AudioJob, JobStatus
from app.providers.storage import ObjectStorage, StorageError
from app.services import jobs
from app.services.upload_rules import object_key, validate_upload

logger = logging.getLogger(__name__)


def initiate_upload(
    db: Session,
    storage: ObjectStorage,
    settings: Settings,
    *,
    filename: str,
    size_bytes: int,
    language_code: str,
) -> tuple[AudioJob, str, str]:
    """Returns (job, signed upload URL, the Content-Type the browser must send)."""
    upload = validate_upload(filename, size_bytes, language_code, settings.max_upload_bytes)
    job_id = uuid.uuid4()
    key = object_key(job_id, upload.extension)
    # Sign before inserting: signing is pure computation, so nothing external is touched if the insert then fails.
    url = storage.create_upload_url(key, upload.mime_type, upload.size_bytes, settings.upload_url_expires_seconds)
    job = jobs.create_job(
        db,
        job_id=job_id,
        original_filename=upload.original_filename,
        object_key=key,
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        language_code=upload.language_code,
    )
    log_event(logger, "upload_created", job_id=job.id, size_bytes=job.size_bytes, mime_type=job.mime_type)
    return job, url, upload.mime_type


def complete_upload(db: Session, storage: ObjectStorage, job_id: uuid.UUID) -> tuple[AudioJob, bool]:
    """Confirm the browser's upload really landed in storage. Returns (job, this_call_made_the_transition).

    Safe to call twice, or concurrently: only the call that wins the guarded UPLOADING -> UPLOADED transition
    reports True. Phase 3 will enqueue the processing task only for that caller, which is what stops a duplicate
    /complete from creating two processing runs.
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
    db.refresh(job)  # the guarded UPDATE bypassed the ORM, so reload what is now in the row
    if moved:
        log_event(logger, "upload_completed", job_id=job.id, size_bytes=job.size_bytes)
    return job, moved
