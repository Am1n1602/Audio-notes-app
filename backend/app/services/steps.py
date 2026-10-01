"""One processing step for a job, shared by both ways of running it: the Celery task (local development) and the
Cloud Run step endpoint (production). One function, so the two entry points cannot drift apart.

A whole transcription is a chain of short steps: each runs, records when the next is due, and enqueues it.
"""

import logging
import uuid

from sqlalchemy.exc import OperationalError

from app.core import failures
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.failures import Failure
from app.db.session import get_sessionmaker
from app.providers.gnani import get_gnani
from app.providers.llm import get_llm
from app.providers.queue import JobQueue, QueueError
from app.providers.storage import get_storage
from app.services import jobs, pipeline
from app.services.summary_prompt import get_prompts

logger = logging.getLogger(__name__)


def assert_ready(settings: Settings) -> None:
    """Fail when a step-running process starts, not on the first job."""
    if missing := settings.missing_worker_secrets():
        raise RuntimeError(f"cannot run steps: {', '.join(missing)} not set")
    get_prompts()  # the prompt file missing or malformed


def run_step(job_id: uuid.UUID, queue: JobQueue) -> None:
    """Run one bounded step for this job, then schedule the next if more is due. Never loops or sleeps long.

    Raises sqlalchemy OperationalError (database blip) or QueueError (next step not enqueued): the caller retries the
    whole step, which is safe because steps are idempotent. Any other exception is a bug: it is recorded on the job,
    logged with its traceback, and re-raised so the caller can see it.
    """
    with get_sessionmaker()() as db:
        try:
            countdown = pipeline.process_job(db, get_gnani(), get_storage(), get_llm(), get_settings(), job_id)
        except AppError as exc:  # JOB_NOT_FOUND: the row is gone, so there is nothing to do
            logger.warning("event=process_job_skipped job_id=%s reason=%s", job_id, exc.code)
            return
        except OperationalError:
            raise  # a database blip: the caller repeats the step
        except Exception:
            # A bug or an unforeseen failure. Record it on the job so it is never left "transcribing" forever.
            logger.exception("event=process_job_crashed job_id=%s", job_id)
            db.rollback()
            jobs.fail_job(
                db,
                job_id,
                Failure(failures.INTERNAL_ERROR, "Something went wrong while processing this recording. Please retry."),
            )
            raise
        if countdown is not None:
            # Record when the next step is due BEFORE enqueueing it, so a duplicate chain's step (which arrives at a
            # different time) can tell it is not the one this job is waiting for (jobs.is_step_due).
            jobs.set_next_step(db, job_id, countdown)
            try:
                queue.enqueue_process_job(job_id, countdown)
            except QueueError:
                jobs.clear_next_step(db, job_id)  # nothing is scheduled, so the repeat of this step must run
                raise
