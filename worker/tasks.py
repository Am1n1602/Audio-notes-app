import logging
import uuid

from celery.signals import worker_ready
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.providers.queue import PROCESS_JOB_TASK, CeleryJobQueue, QueueError
from app.services import recovery, steps
from worker.celery_app import celery_app

logger = logging.getLogger(__name__)
queue = CeleryJobQueue(celery_app)  # the worker re-schedules itself through the same queue adapter the API uses
steps.assert_ready(get_settings())  # fail at startup, not on the first job


@worker_ready.connect
def resume_unfinished_jobs(**_: object) -> None:
    """A worker that was killed takes its in-flight step with it (the message stays 'unacknowledged' in Redis until
    the visibility timeout). Postgres still knows which jobs are mid-flight, so schedule a fresh step for each."""
    try:
        with get_sessionmaker()() as db:
            count = recovery.requeue_unfinished_jobs(db, queue)
    except (OperationalError, QueueError):
        # Not fatal: the worker still serves new jobs, and these jobs are re-enqueued the next time one starts.
        logger.exception("event=worker_resume_failed")
        return
    logger.info("event=worker_resumed jobs=%s", count)


@celery_app.task(name="worker.ping")
def ping() -> str:
    """Boot check only: proves the worker is connected to Redis and can run a task."""
    return "pong"


@celery_app.task(
    name=PROCESS_JOB_TASK,
    # Postgres or Redis briefly unavailable: repeat the step (it is idempotent) with growing, jittered pauses.
    # Bounded: after 10 tries the task stops. Provider errors are NOT retried here; the service handles those.
    autoretry_for=(OperationalError, QueueError),
    retry_backoff=5,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=10,
)
def process_job(job_id: str) -> None:
    """One bounded processing step for a job (services/steps.py). No task outlives Redis' visibility timeout."""
    steps.run_step(uuid.UUID(job_id), queue)
