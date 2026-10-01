"""Getting a job's step chain going again when it has stopped. Two cases, one idea: Postgres holds the truth, so a fresh
step can always be scheduled, and steps are idempotent and guarded by the atomic claim, so a duplicate is harmless.

requeue_unfinished_jobs (local Celery only): a job's next step is a Redis message with a countdown. While a worker
holds it, the message is unacknowledged, and if that worker is killed Redis only hands it back after the visibility
timeout, so at worker start we schedule a fresh step for every job that should have one in flight. Deployed, Cloud
Tasks keeps tasks durably and there is no worker start to hook, so this is not used.

revive_stalled (both): a job silent for too long is given a new step when its owner asks about it, whatever the queue.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.db.models import AudioJob, JobStatus
from app.providers.queue import JobQueue, QueueError
from app.services import jobs

logger = logging.getLogger(__name__)

# Stages where a step of ours is expected to be pending. (UPLOADING is waiting for the browser, not for us.)
RESUMABLE_STATUSES = (JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING)


def looks_stalled(job: AudioJob, after_seconds: int) -> bool:
    """A cheap look at a job already loaded, so asking about a healthy one costs no query. The database decides for real
    in claim_stalled (this clock may differ from the database's by seconds, which does not matter against minutes)."""
    return job.status in RESUMABLE_STATUSES and datetime.now(UTC) - job.updated_at > timedelta(seconds=after_seconds)


def revive_stalled(db: Session, queue: JobQueue, settings: Settings, candidates: list[AudioJob]) -> list[uuid.UUID]:
    """Start a fresh step for each of these jobs whose steps have stopped.

    Why: a job advances as a chain of steps, each scheduling the next. If the chain breaks (a database outage outlasting
    the worker's retries, a lost message) the job would sit "in progress" for ever, with nothing for the person to
    press. Restart recovery above only helps if a worker restarts. So when a job's owner asks about a job that has
    been silent for settings.job_stalled_after_seconds, it is given a new step. Safe to do on a healthy job too: a step
    that arrives while another chain is running is dropped by jobs.is_step_due. Returns the jobs revived.
    """
    stalled = [job.id for job in candidates if looks_stalled(job, settings.job_stalled_after_seconds)]
    revived = jobs.claim_stalled(db, stalled, settings.job_stalled_after_seconds)
    for job_id in revived:
        try:
            queue.enqueue_process_job(job_id)
        except QueueError:
            log_event(
                logger, "job_revive_failed", job_id=job_id
            )  # the broker is down: the next ask, later, tries again
            continue
        log_event(logger, "job_revived", job_id=job_id)
    return revived


def requeue_unfinished_jobs(db: Session, queue: JobQueue) -> int:
    """Enqueue one processing step per unfinished job. Returns how many were enqueued."""
    job_ids = list(db.scalars(select(AudioJob.id).where(AudioJob.status.in_(RESUMABLE_STATUSES))))
    for job_id in job_ids:
        # The fresh step must not be mistaken for a duplicate of the chain that just died. If that chain is in fact
        # alive (a second replica), its next step arrives while this new chain's is recorded as due later, and the
        # one that is early ends (jobs.is_step_due), so exactly one chain is left.
        jobs.clear_next_step(db, job_id)
        queue.enqueue_process_job(job_id)
    return len(job_ids)
