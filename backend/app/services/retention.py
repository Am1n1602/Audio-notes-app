"""How long an uploaded recording stays in storage: at most settings.audio_retention_seconds (2 hours by default).

Two mechanisms, one function. Every upload gets its own delayed task at /initiate (the cap), and a daily sweep deletes
whatever is overdue anyway (the safety net: a task that was lost or kept failing, and uploads that pre-date the rule).
Only the audio goes. The transcript and the summary stay, so the history keeps its meaning.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.db.models import AudioJob
from app.providers.storage import ObjectStorage
from app.services import jobs

logger = logging.getLogger(__name__)

SWEEP_BATCH = 500  # one daily call; whatever is left is picked up by the next one (a request has 5 minutes)


def expire_recording(db: Session, storage: ObjectStorage, job_id: uuid.UUID) -> bool:
    """Delete this job's stored audio and note it on the job. True if this call did it.

    Idempotent: a repeated, duplicate or late call finds the job already marked and does nothing. The object is deleted
    BEFORE the job is marked, so a storage outage (StorageError, which the caller retries) never leaves a job saying the
    file is gone while it is still there. A job that no longer exists has nothing to delete. A job still waiting for
    its upload simply has no object yet: deleting nothing is fine, and /complete refuses it from then on.
    """
    job = db.get(AudioJob, job_id)
    if job is None or job.audio_deleted_at is not None:
        return False
    storage.delete(job.object_key)
    marked = jobs.mark_audio_deleted(db, job_id)
    if marked:
        log_event(logger, "recording_deleted", job_id=job_id)
    return marked


def expire_overdue(
    db: Session, storage: ObjectStorage, settings: Settings, limit: int = SWEEP_BATCH
) -> tuple[int, int]:
    """The daily sweep: delete the audio of every job older than the retention. Returns (deleted, failed).

    One failing delete is counted and skipped, not allowed to stop the rest; the next sweep tries it again."""
    deleted = failed = 0
    for job_id in jobs.overdue_audio_ids(db, timedelta(seconds=settings.audio_retention_seconds), limit):
        try:
            deleted += expire_recording(db, storage, job_id)
        except Exception:  # storage down for this key, or a database blip: leave it for the next sweep
            logger.exception("event=recording_delete_failed job_id=%s", job_id)
            # A failed statement leaves Postgres' transaction aborted (and a dropped connection leaves the session
            # waiting for a rollback): without this, every later job in the batch would fail too, untried.
            db.rollback()
            failed += 1
    log_event(logger, "expiry_sweep_done", deleted=deleted, failed=failed)
    return deleted, failed
