import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.failures import RETRYABLE_CODES, RETRYABLE_WITHOUT_AUDIO
from app.db.models import JobStatus
from app.schemas.summary import Summary
from app.services.upload_rules import DEFAULT_LANGUAGE


class InitiateRequest(BaseModel):
    filename: str
    size_bytes: int
    language_code: str = DEFAULT_LANGUAGE  # one Gnani Batch code, or up to three comma-separated for auto-detection


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
    audio_deleted_at: datetime | None  # set once the recording itself is gone; the transcript and summary stay

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_retry(self) -> bool:
        """Whether the UI should offer a Retry button: only for failures where trying again can plausibly help, and
        not when it would need a recording that has been deleted (a summary retry only needs the saved transcript)."""
        codes = RETRYABLE_CODES if self.audio_deleted_at is None else RETRYABLE_WITHOUT_AUDIO
        return self.status is JobStatus.FAILED and self.error_code in codes


class UploadDetail(UploadListItem):
    mime_type: str
    transcript: str | None
    summary: Summary | None


class AudioLink(BaseModel):
    """A short-lived signed link to the stored recording, for the audio player. A bearer credential while valid."""

    url: str
    expires_in_seconds: int
