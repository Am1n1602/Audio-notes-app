"""Restart recovery: after a worker (re)starts, re-enqueue every job that should have work in flight.

Why this is needed: a job's next step is a Redis message with a countdown. While a worker holds it, the message is
unacknowledged, and if that worker is killed Redis only hands it back after the visibility timeout. Postgres still
holds the truth (the job is mid-transcription), so at startup we simply schedule a fresh step for every such job.
Steps are idempotent and guarded by the atomic claim, so a duplicate step is harmless.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AudioJob, JobStatus
from app.providers.queue import JobQueue
from app.services import jobs

# Stages where a step of ours is expected to be pending. (UPLOADING is waiting for the browser, not for us.)
RESUMABLE_STATUSES = (JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING)


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
