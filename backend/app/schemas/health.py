from typing import Literal

from pydantic import BaseModel

Check = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Check
    redis: Check
