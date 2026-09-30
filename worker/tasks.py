import logging
import uuid

from celery.signals import worker_ready
from sqlalchemy.exc import OperationalError

from app.core import failures
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.failures import Failure
from app.db.session import get_sessionmaker
from app.providers.gnani import get_gnani
from app.providers.llm import get_llm
from app.providers.queue import PROCESS_JOB_TASK, CeleryJobQueue, QueueError
from app.providers.storage import get_storage
from app.services import jobs, pipeline, recovery
from app.services.summary_prompt import get_prompts
from worker.celery_app import celery_app

logger = logging.getLogger(__name__)
queue = CeleryJobQueue(celery_app)  # the worker re-schedules itself through the same queue adapter the API uses
if _missing := get_settings().missing_worker_secrets():  # fail at startup, not on the first job
    raise RuntimeError(f"the worker cannot start: {', '.join(_missing)} not set")
get_prompts()  # fail at startup, not on the first job, if the prompt file is missing or malformed


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
    """One bounded processing step for a job, then re-schedule itself if more is due. Never loops or sleeps long:
    a whole transcription is a chain of short tasks, so no task outlives Redis' visibility timeout."""
    job_uuid = uuid.UUID(job_id)
    with get_sessionmaker()() as db:
        try:
            countdown = pipeline.process_job(db, get_gnani(), get_storage(), get_llm(), get_settings(), job_uuid)
        except AppError as exc:  # JOB_NOT_FOUND: the row is gone, so there is nothing to do
            logger.warning("event=process_job_skipped job_id=%s reason=%s", job_id, exc.code)
            return
        except OperationalError:
            raise  # a database blip: autoretry_for repeats the step
        except Exception:
            # A bug or an unforeseen failure. Record it on the job so it is never left "transcribing" forever, then
            # re-raise so the traceback is logged and the task is marked failed. (Not swallowed.)
            logger.exception("event=process_job_crashed job_id=%s", job_id)
            db.rollback()
            jobs.fail_job(
                db,
                job_uuid,
                Failure(failures.INTERNAL_ERROR, "Something went wrong while processing this recording. Please retry."),
            )
            raise
        if countdown is not None:
            # Record when the next step is due BEFORE enqueueing it, so a duplicate chain's step (which arrives at a
            # different time) can tell it is not the one this job is waiting for (jobs.is_step_due).
            jobs.set_next_step(db, job_uuid, countdown)
            try:
                queue.enqueue_process_job(job_uuid, countdown)
            except QueueError:
                jobs.clear_next_step(db, job_uuid)  # nothing is scheduled, so the autoretry of this task must run
                raise  # QueueError triggers autoretry_for
