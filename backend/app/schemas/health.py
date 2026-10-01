from typing import Literal

from pydantic import BaseModel

Check = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Check
    redis: Check | None = None  # only when the queue is Redis; Cloud Tasks is a Google service, not ours to probe
