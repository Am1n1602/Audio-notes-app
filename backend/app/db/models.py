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

    # Which browser created this job: the SHA-256 (hex) of the random client id that browser generated and sends in the
    # X-Client-Id header. Hashed so a database leak does not hand out working ids. NULL rows belong to nobody and are
    # never shown. Not authentication: it only keeps one browser's history out of another's (app/api/owner.py).
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

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
    # Set by the atomic claim before Create Job is called. "claimed but no gnani_job_id" therefore means the
    # outcome of Create is unknown (crash, lost reply), and the worker must NOT create again. Also the clock for
    # gnani_max_transcribe_seconds.
    submit_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # When this job's next worker step is due (DB clock). A job has ONE chain of self-rescheduling steps, but a worker
    # restart, a second replica or a redelivered message can start another. A step that arrives well before this time
    # is such a duplicate and ends its chain (services/jobs.py: is_step_due). NULL means "a step is due right now".
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )  # {overview, key_points, action_items, ...}
    # A long transcript is summarised in parts, one LLM call per worker step. The finished part-summaries are kept
    # here (in order) so a restart resumes at the next part instead of paying for the first ones again. Cleared once
    # the final summary is saved.
    summary_partials: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
