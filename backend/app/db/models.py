import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Float, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobStatus(enum.StrEnum):
    """Application status. Provider status lives separately in AudioJob.gnani_status."""

    UPLOADING = "UPLOADING"  # row exists and an upload URL was issued; bytes not confirmed yet
    UPLOADED = "UPLOADED"  # object verified in storage
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AudioJob(Base):
    """One uploaded recording and everything we learn about it. Postgres is the source of truth."""

    __tablename__ = "audio_jobs"
    # One explicit CHECK, generated from the enum so the two cannot drift. (The Enum type's own implicit
    # constraint is switched off below: with it on, autogenerate emitted the constraint twice.)
    __table_args__ = (
        CheckConstraint("status IN (" + ", ".join(f"'{s.value}'" for s in JobStatus) + ")", name="job_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    original_filename: Mapped[str] = mapped_column(String(255))  # display only, never used as a storage path
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    language_code: Mapped[str] = mapped_column(String(32))

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20, create_constraint=False, name="job_status"),
        index=True,
    )
    progress_percent: Mapped[int | None] = mapped_column(Integer)
    progress_message: Mapped[str | None] = mapped_column(String(255))

    # One Gnani job per audio job, ever: a retried submission must never leave a second job behind.
    # The unique constraint enforces it in the database; NULLs do not collide.
    gnani_job_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    gnani_status: Mapped[str | None] = mapped_column(String(32))

    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # {overview, key_points, action_items, ...}

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
