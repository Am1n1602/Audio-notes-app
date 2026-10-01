import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from app.core.errors import AppError
from app.providers.queue import JobQueue, QueueError, get_queue
from app.providers.storage import StorageError
from app.services import steps

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])


class StepRequest(BaseModel):
    job_id: uuid.UUID


@router.post("/steps")
def run_step(body: StepRequest, queue: JobQueue = Depends(get_queue)) -> dict[str, str]:
    """Run one step of a job. Called only by Cloud Tasks; Cloud Run lets nothing else in (the service is private).

    The status code is the answer to "should Cloud Tasks deliver this again?": 2xx no, anything else yes.
    """
    try:
        steps.run_step(body.job_id, queue)
    except (OperationalError, QueueError) as exc:
        # The database or the queue blipped. Nothing was lost: deliver the step again later.
        raise AppError(503, "STEP_RETRY", "The step could not finish; it will be delivered again.") from exc
    except Exception:
        # A bug. run_step logged the traceback and failed the job, visibly and retryable by the person; delivering the
        # same step again would only repeat the crash.
        return {"status": "failed"}
    return {"status": "done"}


@router.post("/expire")
def expire_recording(body: StepRequest) -> dict[str, str]:
    """Delete one job's stored audio. Called by Cloud Tasks, at the time scheduled when the upload began."""
    try:
        steps.run_expiry(body.job_id)
    except (OperationalError, StorageError) as exc:
        raise AppError(503, "STEP_RETRY", "The deletion could not finish; it will be delivered again.") from exc
    except Exception:
        logger.exception(
            "event=recording_expiry_crashed job_id=%s", body.job_id
        )  # a retry would repeat it; the sweep tries
        return {"status": "failed"}
    return {"status": "done"}


@router.post("/expire-overdue")
def expire_overdue() -> dict[str, int]:
    """The daily sweep. Called by Cloud Scheduler. 503 only when the database cannot be read at all; one object that
    cannot be deleted is counted in `failed` and left for tomorrow."""
    try:
        deleted, failed = steps.run_sweep()
    except OperationalError as exc:
        raise AppError(503, "STEP_RETRY", "The sweep could not run; Cloud Scheduler will try again.") from exc
    return {"deleted": deleted, "failed": failed}
