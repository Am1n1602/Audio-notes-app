import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.db.models import JobStatus


class InitiateRequest(BaseModel):
    filename: str
    size_bytes: int
    language_code: str = "en-IN"  # one Gnani Batch code, or up to three comma-separated for auto-detection


class UploadTarget(BaseModel):
    """Everything the browser needs to PUT the file straight to storage."""

    url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]  # must be sent exactly as given: they are part of the signature
    expires_in_seconds: int


class InitiateResponse(BaseModel):
    id: uuid.UUID
    status: JobStatus
    upload: UploadTarget


class Summary(BaseModel):
    """The structure the LLM summary must follow."""

    overview: str
    key_points: list[str]
    action_items: list[str]
    decisions: list[str]
    uncertainties: list[str]


class UploadListItem(BaseModel):
    """What the history list needs. No transcript or summary text, so the list stays small."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    size_bytes: int
    duration_seconds: float | None
    language_code: str
    status: JobStatus
    progress_message: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class UploadDetail(UploadListItem):
    mime_type: str
    transcript: str | None
    summary: Summary | None
