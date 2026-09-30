"""The single entry point for a job's background work: routes to the right step by the job's status.

Keeping the routing here means transcription.py knows nothing about summaries and summarization.py knows nothing
about Gnani: each stage is one small module, and this file is the only place that knows the order.
"""

import logging
import time
import uuid

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_event
from app.db.models import JobStatus
from app.providers.gnani import GnaniApi
from app.providers.llm import SummaryLlm
from app.providers.storage import ObjectStorage
from app.services import jobs, summarization, transcription
from app.services.transcription import NextStep, Sleep

logger = logging.getLogger(__name__)


def process_job(
    db: Session,
    gnani: GnaniApi,
    storage: ObjectStorage,
    llm: SummaryLlm,
    settings: Settings,
    job_id: uuid.UUID,
    *,
    sleep: Sleep = time.sleep,
) -> NextStep:
    """Run one step for this job. Returns seconds until the next step is due, or None if nothing more is due."""
    job = jobs.get_job(db, job_id)
    if not jobs.is_step_due(db, job_id):
        log_event(logger, "duplicate_step_dropped", job_id=job_id)
        return None  # another chain of steps is already running this job: end this one
    # The reads above opened a transaction. End it before the network calls below (Gnani, Groq, sleeping between
    # retries), which can take minutes: a connection left "idle in transaction" that long is killed by managed
    # Postgres, and the step would then fail after the paid call was made. The loaded job stays usable.
    db.commit()
    if job.status is JobStatus.SUMMARIZING:
        return summarization.step(db, llm, settings, job, sleep)
    return transcription.process_job(db, gnani, storage, settings, job_id, sleep=sleep)
