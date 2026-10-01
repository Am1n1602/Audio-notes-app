"""Turns an uploaded job into a transcript, one bounded step at a time.

process_job() does ONE step and returns how many seconds until the next step is due (or None when done). The caller
(the Celery task locally, the private Cloud Run step endpoint when deployed) reschedules it. Every step re-reads the
job from Postgres and acts on what it finds there, so a step can be repeated, crash halfway, or be delivered twice
without harm. State lives in the database, never in worker memory.

What a step does, by the job's status:
  QUEUED        claim atomically -> Create -> save the Gnani job id at once -> Start
  TRANSCRIBING  poll Gnani; when it finishes, download the transcript and save it (or fail with a clear reason)
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings
from app.core.failures import Failure
from app.core.logging import log_event
from app.db.models import AudioJob, JobStatus
from app.providers.gnani import (
    SUCCESS_STATUSES,
    TERMINAL_STATUSES,
    BatchFile,
    BatchJob,
    GnaniAmbiguousCreateError,
    GnaniApi,
    GnaniError,
    GnaniPermanentError,
    GnaniProtocolError,
    GnaniTransientError,
)
from app.providers.storage import ObjectStorage, StorageError
from app.services import jobs
from app.services.upload_rules import MAX_AUDIO_SECONDS

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_RETRY_SECONDS = 30  # wait before repeating a step whose Gnani call failed transiently (5xx, network)
RATE_LIMIT_RETRY_SECONDS = 5  # a 429 clears within seconds (Gnani allows ~1 call/s), so do not wait 30 s for it
BACKOFF_SECONDS = (2, 4, 8, 16)  # pauses between attempts of a Create/Start that provably did not land
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1  # so at most 5 tries, then the job fails visibly instead of looping

Sleep = Callable[[float], None]
NextStep = int | None  # seconds until the next step is due; None means nothing more is due
SUMMARIZE_NOW = 0  # the transcript was just saved: run the summary step straight away


def process_job(
    db: Session,
    gnani: GnaniApi,
    storage: ObjectStorage,
    settings: Settings,
    job_id: uuid.UUID,
    *,
    sleep: Sleep = time.sleep,
) -> NextStep:
    """Run one step for this job. Returns seconds until the next step is due, or None if nothing more is due."""
    job = jobs.get_job(db, job_id)
    if job.status is JobStatus.UPLOADED:
        # The API enqueues first and marks QUEUED second, so the worker can arrive before that second write.
        # Whichever side gets there first wins; the other sees False and carries on.
        jobs.transition(db, job.id, JobStatus.UPLOADED, JobStatus.QUEUED)
        db.refresh(job)
    if job.status is JobStatus.QUEUED:
        return _submit(db, gnani, storage, settings, job, sleep)
    if job.status is JobStatus.TRANSCRIBING:
        return _poll(db, gnani, storage, settings, job, sleep)
    return None  # UPLOADING, SUMMARIZING, COMPLETED, FAILED: not this step's business


# --- submitting ----------------------------------------------------------------------------------------------


def _submit(
    db: Session, gnani: GnaniApi, storage: ObjectStorage, settings: Settings, job: AudioJob, sleep: Sleep
) -> NextStep:
    # 1. Claim. Only one caller wins, so two workers or a redelivered task cannot both call Create.
    if not jobs.transition(db, job.id, JobStatus.QUEUED, JobStatus.TRANSCRIBING, submit_started_at=func.now()):
        return None

    # 2. Create. Repeated ONLY when the request provably never reached Gnani (connection failure, 429).
    url = storage.create_download_url(job.object_key, settings.gnani_source_url_expires_seconds)
    try:
        gnani_job_id = _attempts(lambda: gnani.create_batch_job(url, job.language_code), sleep, job, "create")
    except GnaniError as exc:
        return _fail(db, job, failure_from_error(exc), exc)

    # 3. Save the id in its own transaction before anything else can go wrong.
    if not jobs.set_gnani_job_id(db, job.id, gnani_job_id):
        # Claimed, created, but the job was failed or changed in between. The Gnani job is now unreferenced.
        log_event(logger, "gnani_job_orphaned", job_id=job.id, gnani_job_id=gnani_job_id)
        return None
    db.refresh(job)  # the guarded UPDATEs bypassed the ORM; reload so later log lines carry the Gnani job id
    log_event(logger, "transcription_submitted", job_id=job.id, gnani_job_id=gnani_job_id)

    # 4. Start. Safe to repeat. If it still fails, the next poll notices the job never started and starts it.
    try:
        _attempts(lambda: gnani.start_batch_job(gnani_job_id), sleep, job, "start")
    except GnaniTransientError:
        log_event(logger, "start_deferred", job_id=job.id, gnani_job_id=gnani_job_id)
    except GnaniError as exc:
        return _fail(db, job, failure_from_error(exc), exc)
    return settings.gnani_poll_seconds  # wait one interval first: Gnani rate-limits calls made right after Start


def _attempts(call: Callable[[], T], sleep: Sleep, job: AudioJob, what: str) -> T:
    """Call up to MAX_ATTEMPTS times, pausing between tries, but only for transient errors. Anything else propagates
    at once. (GnaniAmbiguousCreateError is not transient, so an unknown Create outcome is never retried.)"""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except GnaniTransientError as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            log_event(logger, "gnani_call_retry", job_id=job.id, call=what, attempt=attempt + 1, error=exc)
            sleep(BACKOFF_SECONDS[attempt])
    raise AssertionError("unreachable")


# --- polling -------------------------------------------------------------------------------------------------


def _poll(
    db: Session, gnani: GnaniApi, storage: ObjectStorage, settings: Settings, job: AudioJob, sleep: Sleep
) -> NextStep:
    if job.gnani_job_id is None:
        # Claimed, but no Gnani job id was ever saved: a crash or a lost reply, so we do not know whether Gnani made
        # a job. Creating again could duplicate it, so fail visibly and let the user decide to retry.
        return _fail(db, job, failure_from_error(GnaniAmbiguousCreateError("no job id recorded after claim")), None)

    expired = _elapsed_seconds(job) > settings.gnani_max_transcribe_seconds
    try:
        batch = gnani.get_batch_job(job.gnani_job_id)
    except GnaniTransientError as exc:
        return _transient(db, job, exc, expired, "gnani_poll_failed")
    except GnaniError as exc:
        return _fail(db, job, failure_from_error(exc), exc)

    jobs.record_provider_status(db, job.id, batch.status)
    jobs.set_progress_message(
        db, job.id, JobStatus.TRANSCRIBING, jobs.PROGRESS_MESSAGES[JobStatus.TRANSCRIBING]
    )  # it answered
    if batch.status == "CREATED":
        # Never started: we crashed between Create and Start, or Start kept failing. Start is safe to repeat, but not
        # forever: the deadline applies here too, or a Start that always fails would poll for the rest of time.
        if expired:
            return _timeout(db, job)
        try:
            gnani.start_batch_job(job.gnani_job_id)
        except GnaniTransientError:
            pass  # try again on the next poll
        except GnaniError as exc:
            return _fail(db, job, failure_from_error(exc), exc)
        return settings.gnani_poll_seconds
    if batch.status not in TERMINAL_STATUSES:
        if expired:
            return _timeout(db, job)
        return settings.gnani_poll_seconds
    return _finish(db, gnani, storage, job, batch, expired)


def _timeout(db: Session, job: AudioJob) -> NextStep:
    return _fail(
        db, job, Failure(failures.TRANSCRIPTION_TIMEOUT, "Transcription took too long. Please try again."), None
    )


def _transient(db: Session, job: AudioJob, exc: GnaniTransientError, expired: bool, event: str) -> NextStep:
    """A Gnani call failed transiently: try the step again later, unless the deadline has passed (then fail visibly,
    so no path can retry forever). A job Gnani already finished is still collected after the deadline: only a
    call that keeps failing is given up on."""
    log_event(logger, event, job_id=job.id, gnani_job_id=job.gnani_job_id, error=exc)
    if expired:
        return _fail(db, job, failures_unavailable(), exc)
    if exc.status_code != 429:  # a 429 clears in seconds; anything else means Gnani is not answering: say so
        jobs.set_progress_message(db, job.id, JobStatus.TRANSCRIBING, jobs.WAITING_FOR_GNANI_MESSAGE)
    return _retry_delay(exc)


def _retry_delay(exc: GnaniTransientError) -> int:
    return RATE_LIMIT_RETRY_SECONDS if exc.status_code == 429 else TRANSIENT_RETRY_SECONDS


def _elapsed_seconds(job: AudioJob) -> float:
    if job.submit_started_at is None:
        return 0.0
    return (datetime.now(UTC) - job.submit_started_at).total_seconds()


# --- finishing -----------------------------------------------------------------------------------------------


def _finish(
    db: Session, gnani: GnaniApi, storage: ObjectStorage, job: AudioJob, batch: BatchJob, expired: bool
) -> NextStep:
    assert job.gnani_job_id is not None
    ready: BatchFile | None = None
    try:
        # The files call hands out a FRESH transcript link (they expire after an hour), so it is fetched now and
        # used at once, never stored.
        files = gnani.get_batch_files(job.gnani_job_id)
        if batch.status in SUCCESS_STATUSES:
            ready = next((f for f in files if f.status == "COMPLETED" and f.transcript_url), None)
    except GnaniTransientError as exc:
        return _transient(db, job, exc, expired, "gnani_files_failed")  # else the next step polls and comes back here
    except GnaniError as exc:
        return _fail(db, job, failure_from_error(exc), exc)

    if ready is None or ready.transcript_url is None:
        return _fail(db, job, _if_recording_is_gone(storage, job, failure_from_terminal(batch, files)), None)

    try:
        transcript = gnani.download_transcript(ready.transcript_url)
    except GnaniTransientError as exc:
        return _transient(db, job, exc, expired, "transcript_download_failed")
    except GnaniError as exc:  # a refused or malformed download must not be mistaken for a credentials problem
        return _fail(
            db,
            job,
            Failure(failures.TRANSCRIPTION_FAILED, "The transcript could not be retrieved. Please try again."),
            exc,
        )

    if not transcript.text.strip():
        return _fail(db, job, empty_transcript_failure(), None)

    saved = jobs.transition(
        db,
        job.id,
        JobStatus.TRANSCRIBING,
        JobStatus.SUMMARIZING,
        transcript=transcript.text,
        duration_seconds=transcript.duration_seconds,
        gnani_status=batch.status,
    )
    if saved:
        log_event(
            logger, "transcript_saved", job_id=job.id, gnani_job_id=job.gnani_job_id, characters=len(transcript.text)
        )
    return SUMMARIZE_NOW if saved else None  # the summary step takes over from SUMMARIZING


# --- failures ------------------------------------------------------------------------------------------------


def _fail(db: Session, job: AudioJob, failure: Failure, cause: Exception | None) -> NextStep:
    """Fail the job. Always returns None ("nothing more is due"), so a step can end with `return _fail(...)`."""
    if jobs.fail_job(db, job.id, failure):
        log_event(
            logger,
            "job_failed",
            job_id=job.id,
            gnani_job_id=job.gnani_job_id,
            error_code=failure.code,
            cause=type(cause).__name__ if cause else None,
        )
    return None


def _if_recording_is_gone(storage: ObjectStorage, job: AudioJob, failure: Failure) -> Failure:
    """Gnani could not download the recording. If that is because it is no longer in storage, trying again can never
    work, so say so instead of offering a Retry that fails the same way. (If storage itself cannot answer, we cannot
    tell, and the failure stays retryable.)"""
    if failure.code != failures.SOURCE_UNREACHABLE:
        return failure
    try:
        gone = storage.head(job.object_key) is None
    except StorageError:
        return failure
    return missing_recording_failure() if gone else failure


def missing_recording_failure() -> Failure:
    return Failure(
        failures.RECORDING_MISSING, "The uploaded recording is no longer in storage. Please upload it again."
    )


def failures_unavailable() -> Failure:
    return Failure(
        failures.TRANSCRIPTION_UNAVAILABLE,
        "The transcription service is busy or unavailable. Please try again in a few minutes.",
    )


def empty_transcript_failure() -> Failure:
    return Failure(failures.EMPTY_TRANSCRIPT, "No speech was detected in this recording. Try a different file.")


def failure_from_error(exc: GnaniError) -> Failure:
    """Map a Gnani client error to what the user sees. Provider text stays in the logs, never in the message."""
    if isinstance(exc, GnaniAmbiguousCreateError):
        return Failure(
            failures.SUBMIT_UNCONFIRMED,
            "We could not confirm that transcription started. You can try again; in rare cases this may "
            "start a duplicate request.",
        )
    if isinstance(exc, GnaniTransientError):
        return failures_unavailable()
    if isinstance(exc, GnaniProtocolError):
        return Failure(failures.TRANSCRIPTION_FAILED, "The transcription service returned an unexpected response.")
    if isinstance(exc, GnaniPermanentError):
        if exc.status_code in (401, 403):
            return Failure(
                failures.TRANSCRIPTION_AUTH_FAILED,
                "Transcription is unavailable because of a configuration problem on our side.",
            )
        if exc.status_code in (400, 422):
            return Failure(
                failures.TRANSCRIPTION_REQUEST_REJECTED,
                "The transcription service rejected this request. Check the file type and language.",
            )
    return Failure(failures.TRANSCRIPTION_FAILED, "Transcription failed. Please try again.")


def failure_from_terminal(batch: BatchJob, files: list[BatchFile]) -> Failure:
    """Gnani finished without a usable transcript. The job-level status has no reason of its own (observed live):
    the cause is in cancel_reason or in each file's error_message."""
    reasons = " ".join(filter(None, [batch.cancel_reason, *(f.error_message for f in files)])).lower()
    # These are the phrases Gnani really used in live runs: "no speech detected in 6s of audio" for a silent file and
    # "ffprobe could not read the file: ... Invalid data found when processing input" for a text file named .wav.
    # ("Empty transcript after 3 retries" is the wording its docs give for a related case.)
    if "empty transcript" in reasons or "no speech" in reasons:
        return empty_transcript_failure()
    if "could not read the file" in reasons or "invalid data found" in reasons:
        return Failure(
            failures.INVALID_AUDIO, "This file could not be read as audio. It may be corrupted or not an audio file."
        )
    if "above the" in reasons and "limit" in reasons:  # "audio is 14700.2s, above the 14400s limit" (seen live)
        return Failure(
            failures.RECORDING_TOO_LONG,
            f"This recording is longer than the {MAX_AUDIO_SECONDS // 3600} hour limit. "
            "Split it into shorter parts and upload them one at a time.",
        )
    # a recording deleted from storage: job START_FAILED "All provided paths were invalid", file "FILE_NOT_FOUND"
    if "download" in reasons or "paths were invalid" in reasons or "file_not_found" in reasons:
        return Failure(
            failures.SOURCE_UNREACHABLE, "The transcription service could not download the recording. Please try again."
        )
    if batch.status == "CANCELLED":
        return Failure(failures.TRANSCRIPTION_CANCELLED, "Transcription was cancelled. Please try again.")
    return Failure(failures.TRANSCRIPTION_FAILED, "Transcription failed. Please try again, or upload a different file.")
