import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.failures import RETRYABLE_CODES
from app.db.models import JobStatus
from app.schemas.summary import Summary


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_retry(self) -> bool:
        """Whether the UI should offer a Retry button: only for failures where trying again can plausibly help."""
        return self.status is JobStatus.FAILED and self.error_code in RETRYABLE_CODES


class UploadDetail(UploadListItem):
    mime_type: str
    transcript: str | None
    summary: Summary | None
