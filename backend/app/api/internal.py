import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

from app.core.errors import AppError
from app.providers.queue import JobQueue, QueueError, get_queue
from app.services import steps

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
