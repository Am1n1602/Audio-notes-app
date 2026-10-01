import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.failures import RETRYABLE_CODES
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
    has_transcript: bool = Field(exclude=True)  # only feeds can_retry below; not part of the API

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_retry(self) -> bool:
        """Whether the UI should offer a Retry button. The same rule services/uploads.retry_upload enforces, so the
        button is never offered for a retry that would be refused, nor hidden for one that would work: only for failures
        where trying again can help, and, once the recording has been deleted, only if a transcript was saved (the
        retry then resumes at the summary, which reads the transcript and never the audio)."""
        audio_available = self.audio_deleted_at is None or self.has_transcript
        return self.status is JobStatus.FAILED and self.error_code in RETRYABLE_CODES and audio_available


class UploadDetail(UploadListItem):
    mime_type: str
    transcript: str | None
    summary: Summary | None


class AudioLink(BaseModel):
    """A short-lived signed link to the stored recording, for the audio player. A bearer credential while valid."""

    url: str
    expires_in_seconds: int
