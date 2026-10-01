import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fakes import FakeGnani, FakeStorage
from helpers import job_in_status
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings, get_settings
from app.db.models import AudioJob, JobStatus
from app.db.session import get_sessionmaker
from app.providers.gnani import (
    BatchFile,
    BatchJob,
    GnaniAmbiguousCreateError,
    GnaniPermanentError,
    GnaniProtocolError,
    GnaniTransientError,
    Transcript,
)
from app.services import jobs, transcription

IN_PROGRESS = BatchJob("IN_PROGRESS", None, 1, 0, 0)
COMPLETED = BatchJob("COMPLETED", None, 1, 1, 0)
SETTINGS = get_settings()


def safe_transient() -> GnaniTransientError:  # provably never reached Gnani (429 / connection refused)
    return GnaniTransientError("POST /jobs -> HTTP 429", status_code=429, not_processed=True)


def unsafe_transient() -> GnaniTransientError:  # timed out / 503: may or may not have been processed
    return GnaniTransientError("GET /jobs -> HTTP 503", status_code=503, not_processed=False)


def queued_job(db: Session, *, status: JobStatus = JobStatus.QUEUED) -> uuid.UUID:
    return job_in_status(db, status)


class Harness:
    def __init__(self, db: Session, gnani: FakeGnani, settings: Settings = SETTINGS) -> None:
        self.db, self.gnani, self.settings, self.sleeps = db, gnani, settings, []  # type: ignore[var-annotated]
        self.storage = FakeStorage()

    def step(self, job_id: uuid.UUID) -> int | None:
        self.db.expire_all()  # a real Celery step always starts in a fresh session; do not serve a cached row
        return transcription.process_job(
            self.db, self.gnani, self.storage, self.settings, job_id, sleep=self.sleeps.append
        )

    def job(self, job_id: uuid.UUID) -> AudioJob:
        self.db.expire_all()
        return jobs.get_job(self.db, job_id)


@pytest.fixture
def gnani() -> FakeGnani:
    return FakeGnani()


@pytest.fixture
def h(db: Session, gnani: FakeGnani) -> Harness:
    return Harness(db, gnani)


# --- the happy path, step by step ---------------------------------------------------------------------------


def test_a_queued_job_becomes_a_saved_transcript_in_three_bounded_steps(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.job_script = [IN_PROGRESS, COMPLETED]

    # step 1: claim, create, save the id, start -- then wait one poll interval
    assert h.step(job_id) == 10
    job = h.job(job_id)
    assert (job.status, job.gnani_job_id, job.gnani_status) == (JobStatus.TRANSCRIBING, "gnani-job-1", "CREATED")
    assert job.submit_started_at is not None and job.progress_message == "Transcribing audio…"
    assert gnani.calls == ["create", "start"]
    source_url, language = gnani.created_with[0]
    assert source_url == f"https://storage.test/uploads/{job_id}/audio.wav?download=fake&expires=3600"
    assert language == "hi-IN,en-IN"

    # step 2: Gnani still working -> come back in one interval, nothing else changes
    assert h.step(job_id) == 10
    assert h.job(job_id).gnani_status == "IN_PROGRESS" and h.job(job_id).status is JobStatus.TRANSCRIBING

    # step 3: finished -> transcript saved, moved on; the summary step is due immediately
    assert h.step(job_id) == transcription.SUMMARIZE_NOW
    job = h.job(job_id)
    assert (job.status, job.transcript, job.duration_seconds) == (JobStatus.SUMMARIZING, "hello world", 6.0)
    assert (job.gnani_status, job.progress_message) == ("COMPLETED", "Generating summary…")
    assert gnani.calls.count("create") == 1  # one Gnani job for the whole run


def test_a_finished_or_unrelated_job_is_left_alone(h: Harness, gnani: FakeGnani) -> None:
    for status in (JobStatus.SUMMARIZING, JobStatus.UPLOADING):
        assert h.step(queued_job(h.db, status=status)) is None
    assert gnani.calls == []


def test_the_worker_can_arrive_before_the_api_marks_the_job_queued(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db, status=JobStatus.UPLOADED)  # the API enqueues first, marks QUEUED second
    assert h.step(job_id) == 10
    assert h.job(job_id).status is JobStatus.TRANSCRIBING and gnani.calls == ["create", "start"]


# --- exactly one Gnani job -------------------------------------------------------------------------------------


def test_a_restarted_worker_resumes_polling_and_never_creates_again(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db, status=JobStatus.TRANSCRIBING)
    h.db.execute(text("UPDATE audio_jobs SET gnani_job_id = 'gnani-existing', submit_started_at = now()"))
    h.db.commit()
    assert h.step(job_id) == transcription.SUMMARIZE_NOW  # the fake says COMPLETED
    assert "create" not in gnani.calls and h.job(job_id).gnani_job_id == "gnani-existing"
    assert h.job(job_id).status is JobStatus.SUMMARIZING


def test_claimed_but_no_id_means_the_outcome_is_unknown_so_it_fails_instead_of_creating(
    h: Harness, gnani: FakeGnani
) -> None:
    job_id = queued_job(h.db, status=JobStatus.TRANSCRIBING)  # e.g. the worker died between claim and Create
    h.db.execute(text("UPDATE audio_jobs SET submit_started_at = now()"))
    h.db.commit()
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.SUBMIT_UNCONFIRMED)
    assert job.error_code in failures.RETRYABLE_CODES and gnani.calls == []


def test_duplicate_deliveries_racing_create_exactly_one_gnani_job(
    monkeypatch: pytest.MonkeyPatch, db: Session, gnani: FakeGnani
) -> None:
    job_id = queued_job(db)
    barrier = threading.Barrier(6)
    real_get_job = jobs.get_job

    def get_job_then_wait(session: Session, jid: uuid.UUID) -> AudioJob:
        job = real_get_job(session, jid)
        barrier.wait(timeout=10)  # every delivery has now seen QUEUED, before any of them claims the job
        return job

    monkeypatch.setattr(jobs, "get_job", get_job_then_wait)

    def deliver(_: int) -> int | None:
        with get_sessionmaker()() as session:
            return transcription.process_job(session, gnani, FakeStorage(), SETTINGS, job_id, sleep=lambda _s: None)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(deliver, range(6)))
    assert gnani.calls.count("create") == 1  # the atomic claim let one delivery through
    assert results.count(10) == 1 and results.count(None) == 5  # the winner waits to poll; the rest stand down


def test_a_delivery_holding_a_stale_snapshot_cannot_create(h: Harness, gnani: FakeGnani) -> None:
    """Deterministic version of the race: this delivery read QUEUED, but another one claimed the job before it did."""
    job_id = queued_job(h.db)
    stale = jobs.get_job(h.db, job_id)  # this delivery's snapshot: still QUEUED
    with get_sessionmaker()() as other:
        assert jobs.transition(other, job_id, JobStatus.QUEUED, JobStatus.TRANSCRIBING, submit_started_at=func.now())
    assert transcription._submit(h.db, gnani, FakeStorage(), SETTINGS, stale, lambda _s: None) is None
    assert gnani.calls == []  # it lost the claim, so it must not call Create


# --- Create: retry only what provably did not land ------------------------------------------------------------


def test_create_refused_before_processing_is_retried_with_backoff_then_succeeds(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.create_script = [safe_transient(), safe_transient(), "gnani-job-9"]
    assert h.step(job_id) == 10
    assert gnani.calls.count("create") == 3 and h.sleeps == [2, 4]
    assert h.job(job_id).gnani_job_id == "gnani-job-9"


def test_create_retries_are_bounded_and_the_failure_is_visible(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.create_script = [safe_transient()]  # never recovers
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.TRANSCRIPTION_UNAVAILABLE)
    assert gnani.calls.count("create") == 5 and h.sleeps == [2, 4, 8, 16]  # 5 tries, then stop


def test_create_with_an_unknown_outcome_is_never_retried(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.create_script = [GnaniAmbiguousCreateError("Create Job outcome unknown")]
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.SUBMIT_UNCONFIRMED)
    assert gnani.calls == ["create"] and h.sleeps == []  # exactly one attempt, no waiting


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (400, failures.TRANSCRIPTION_REQUEST_REJECTED, False),
        (422, failures.TRANSCRIPTION_REQUEST_REJECTED, False),
        (401, failures.TRANSCRIPTION_AUTH_FAILED, False),
        (403, failures.TRANSCRIPTION_AUTH_FAILED, False),
        (404, failures.TRANSCRIPTION_FAILED, True),
    ],
)
def test_permanent_gnani_errors_fail_at_once_without_retrying(
    h: Harness, gnani: FakeGnani, status_code: int, code: str, retryable: bool
) -> None:
    job_id = queued_job(h.db)
    gnani.create_script = [GnaniPermanentError("POST -> HTTP", status_code=status_code)]
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, code)
    assert (job.error_code in failures.RETRYABLE_CODES) is retryable
    assert gnani.calls == ["create"] and h.sleeps == []


def test_user_visible_messages_carry_no_provider_text_or_secrets(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.create_script = [GnaniPermanentError("POST /jobs -> HTTP 401: INVALID_API_KEY sk-live-123", status_code=401)]
    h.step(job_id)
    message = h.job(job_id).error_message or ""
    assert "sk-live" not in message and "INVALID_API_KEY" not in message and "HTTP" not in message


# --- Start ----------------------------------------------------------------------------------------------------


def test_a_start_that_keeps_failing_is_finished_by_the_next_poll(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.start_script = [safe_transient()]  # Start never works in this step
    gnani.job_script = [BatchJob("CREATED", None, 1, 0, 0), IN_PROGRESS]
    assert h.step(job_id) == 10  # created and saved; Start deferred, job stays TRANSCRIBING
    assert gnani.calls.count("start") == 5 and h.job(job_id).gnani_job_id == "gnani-job-1"
    gnani.start_script = [None]  # Gnani recovers
    assert h.step(job_id) == 10  # the poll sees status CREATED and starts it
    assert gnani.calls.count("start") == 6 and gnani.calls.count("create") == 1


def test_a_permanent_start_error_fails_the_job(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    gnani.start_script = [GnaniPermanentError("POST start -> HTTP 404", status_code=404)]
    assert h.step(job_id) is None and h.job(job_id).status is JobStatus.FAILED


# --- polling --------------------------------------------------------------------------------------------------


def transcribing(h: Harness) -> uuid.UUID:
    job_id = queued_job(h.db, status=JobStatus.TRANSCRIBING)
    h.db.execute(text("UPDATE audio_jobs SET gnani_job_id = 'gnani-job-1', submit_started_at = now()"))
    h.db.commit()
    return job_id


def test_a_transient_poll_failure_is_retried_later_and_recovers(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.job_script = [unsafe_transient(), COMPLETED]
    assert h.step(job_id) == transcription.TRANSIENT_RETRY_SECONDS
    assert h.job(job_id).status is JobStatus.TRANSCRIBING
    assert h.step(job_id) == transcription.SUMMARIZE_NOW and h.job(job_id).status is JobStatus.SUMMARIZING


def test_a_rate_limited_poll_is_retried_in_seconds_not_half_a_minute(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    limited = GnaniTransientError("GET /jobs -> HTTP 429", status_code=429, not_processed=True)
    gnani.job_script = [limited, COMPLETED]
    assert h.step(job_id) == transcription.RATE_LIMIT_RETRY_SECONDS == 5  # a 429 clears within seconds
    gnani.files_script = [limited, [BatchFile("f1", "COMPLETED", "https://s3.test/t.json?sig=x", None)]]
    assert h.step(job_id) == transcription.RATE_LIMIT_RETRY_SECONDS  # the same when the files call is refused
    assert h.step(job_id) == transcription.SUMMARIZE_NOW and h.job(job_id).status is JobStatus.SUMMARIZING


def test_while_gnani_does_not_answer_the_page_says_so_and_goes_back_when_it_does(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    assert h.job(job_id).progress_message == "Transcribing audio…"
    gnani.job_script = [unsafe_transient(), unsafe_transient(), IN_PROGRESS]
    h.step(job_id)
    assert h.job(job_id).progress_message == jobs.WAITING_FOR_GNANI_MESSAGE  # not "Transcribing audio…" for two hours
    h.step(job_id)
    assert h.job(job_id).progress_message == jobs.WAITING_FOR_GNANI_MESSAGE
    h.step(job_id)  # Gnani answers again
    assert h.job(job_id).progress_message == "Transcribing audio…"


def test_a_rate_limited_poll_does_not_change_what_the_page_says(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.job_script = [GnaniTransientError("GET /jobs -> HTTP 429", status_code=429, not_processed=True), IN_PROGRESS]
    h.step(job_id)
    assert h.job(job_id).progress_message == "Transcribing audio…"  # a 429 clears in seconds: nothing to report


def test_a_job_given_up_on_does_not_keep_the_waiting_message(h: Harness, gnani: FakeGnani) -> None:
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    gnani.job_script = [unsafe_transient()]
    h.step(job_id)
    make_old(h, 120)
    h.step(job_id)
    job = h.job(job_id)
    assert (job.status, job.error_code, job.progress_message) == (
        JobStatus.FAILED,
        failures.TRANSCRIPTION_UNAVAILABLE,
        None,
    )


def make_old(h: Harness, seconds: int) -> None:
    h.db.execute(text(f"UPDATE audio_jobs SET submit_started_at = now() - interval '{seconds} seconds'"))
    h.db.commit()


def test_a_job_gnani_never_finishes_times_out(h: Harness, gnani: FakeGnani) -> None:
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    gnani.job_script = [IN_PROGRESS]
    assert h.step(job_id) == 10  # young enough: keep polling
    make_old(h, 120)
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_TIMEOUT
    assert failures.TRANSCRIPTION_TIMEOUT in failures.RETRYABLE_CODES


def test_a_dead_gnani_cannot_keep_a_job_polling_forever(h: Harness, gnani: FakeGnani) -> None:
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    gnani.job_script = [unsafe_transient()]
    assert h.step(job_id) == transcription.TRANSIENT_RETRY_SECONDS
    make_old(h, 120)
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_UNAVAILABLE  # the retry loop is bounded by the deadline


def test_a_job_that_finished_while_the_worker_was_down_is_collected_not_timed_out(h: Harness, gnani: FakeGnani) -> None:
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    make_old(h, 3600)  # long past the deadline, but Gnani did finish
    assert h.step(job_id) == transcription.SUMMARIZE_NOW
    assert h.job(job_id).status is JobStatus.SUMMARIZING


def test_a_job_stuck_in_created_because_start_always_fails_times_out(h: Harness, gnani: FakeGnani) -> None:
    """The CREATED branch used to skip the deadline, so a Start that never worked polled for the rest of time."""
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    gnani.job_script = [BatchJob("CREATED", None, 1, 0, 0)]
    gnani.start_script = [unsafe_transient()]
    assert h.step(job_id) == 10  # young: try Start again next poll
    make_old(h, 120)
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_TIMEOUT


@pytest.mark.parametrize("broken", ["files", "download"])
def test_a_finished_job_whose_result_cannot_be_fetched_does_not_retry_forever(
    h: Harness, gnani: FakeGnani, broken: str
) -> None:
    """Gnani says COMPLETED, but the files call (or the transcript download) keeps failing transiently."""
    h.settings = SETTINGS.model_copy(update={"gnani_max_transcribe_seconds": 60})
    job_id = transcribing(h)
    if broken == "files":
        gnani.files_script = [unsafe_transient()]
    else:
        gnani.transcript_script = [unsafe_transient()]
    assert h.step(job_id) == transcription.TRANSIENT_RETRY_SECONDS  # young: come back and try again
    make_old(h, 120)
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_UNAVAILABLE


# --- how a finished-but-unusable job is explained --------------------------------------------------------------


def file_failed(message: str | None, status: str = "FAILED") -> list[BatchFile]:
    return [BatchFile("f1", status, None, message)]


@pytest.mark.parametrize(
    ("batch", "files", "code"),
    [
        (BatchJob("FAILED", None, 1, 0, 1), file_failed("public download: HTTP 406"), failures.SOURCE_UNREACHABLE),
        (
            BatchJob("START_FAILED", "All provided paths were invalid — nothing to process.", 0, 0, 0),
            [],
            failures.SOURCE_UNREACHABLE,
        ),
        # a recording deleted from the bucket, as Gnani reported it in a live run (Phase 7)
        (
            BatchJob("START_FAILED", None, 0, 0, 0),
            file_failed("FILE_NOT_FOUND", "SKIPPED"),
            failures.SOURCE_UNREACHABLE,
        ),
        (COMPLETED, file_failed("Empty transcript after 3 retries"), failures.EMPTY_TRANSCRIPT),
        # the exact wording Gnani used in live runs (see the comment in failure_from_terminal):
        (
            BatchJob("FAILED", None, 1, 0, 1),
            file_failed("no speech detected in 6s of audio"),
            failures.EMPTY_TRANSCRIPT,
        ),
        (
            BatchJob("FAILED", None, 1, 0, 1),
            file_failed("ffprobe could not read the file: the file: Invalid data found when processing input"),
            failures.INVALID_AUDIO,
        ),
        (
            BatchJob("PARTIAL_FAILURE", None, 1, 0, 1),
            file_failed("Empty transcript after 3 retries"),
            failures.EMPTY_TRANSCRIPT,
        ),
        # a recording past Gnani's 4 hour limit, as Gnani worded it for a real 4 h 05 min upload (Phase 6)
        (
            BatchJob("FAILED", None, 1, 0, 1),
            file_failed("audio is 14700.2s, above the 14400s limit"),
            failures.RECORDING_TOO_LONG,
        ),
        (BatchJob("CANCELLED", "cancelled", 1, 0, 0), file_failed(None, "CANCELLED"), failures.TRANSCRIPTION_CANCELLED),
        (BatchJob("FAILED", None, 1, 0, 1), file_failed("boom"), failures.TRANSCRIPTION_FAILED),
        (BatchJob("FAILED", None, 1, 0, 1), [], failures.TRANSCRIPTION_FAILED),
    ],
)
def test_a_finished_job_without_a_transcript_is_explained_from_the_file_records(
    h: Harness, gnani: FakeGnani, batch: BatchJob, files: list[BatchFile], code: str
) -> None:
    job_id = transcribing(h)
    h.storage.put(h.job(job_id).object_key, 100)  # the recording is in storage (a vanished one has its own test)
    gnani.job_script, gnani.files_script = [batch], [files]
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, code)
    assert job.error_message and job.transcript is None
    assert "download" not in gnani.calls  # nothing to download


DOWNLOAD_REFUSED = [BatchFile("f1", "FAILED", None, "public download: HTTP 404")]
FAILED_JOB = BatchJob("FAILED", None, 1, 0, 1)


def test_a_recording_that_vanished_from_storage_is_not_offered_a_retry(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)  # the job's file is NOT in the (empty) storage
    gnani.job_script, gnani.files_script = [FAILED_JOB], [DOWNLOAD_REFUSED]
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, failures.RECORDING_MISSING)
    assert "upload it again" in (job.error_message or "").lower()


def test_a_download_failure_with_the_recording_still_in_storage_stays_retryable(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    h.storage.put(h.job(job_id).object_key, 100)  # it is there: Gnani's download failed for another reason
    gnani.job_script, gnani.files_script = [FAILED_JOB], [DOWNLOAD_REFUSED]
    h.step(job_id)
    assert h.job(job_id).error_code == failures.SOURCE_UNREACHABLE


def test_when_storage_cannot_say_whether_the_recording_exists_the_failure_stays_retryable(
    h: Harness, gnani: FakeGnani
) -> None:
    job_id = transcribing(h)
    h.storage.outage = True
    gnani.job_script, gnani.files_script = [FAILED_JOB], [DOWNLOAD_REFUSED]
    h.step(job_id)
    assert h.job(job_id).error_code == failures.SOURCE_UNREACHABLE


def test_a_blank_transcript_is_reported_as_no_speech(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.transcript_script = [Transcript("   ", 3.0, "en-IN")]
    assert h.step(job_id) is None
    job = h.job(job_id)
    assert (job.status, job.error_code, job.transcript) == (JobStatus.FAILED, failures.EMPTY_TRANSCRIPT, None)
    assert failures.EMPTY_TRANSCRIPT not in failures.RETRYABLE_CODES  # silence will still be silent


def test_transient_trouble_collecting_the_result_is_retried_not_failed(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.files_script = [unsafe_transient(), [BatchFile("f1", "COMPLETED", "https://s3.test/t.json?sig=x", None)]]
    assert h.step(job_id) == transcription.TRANSIENT_RETRY_SECONDS and h.job(job_id).status is JobStatus.TRANSCRIBING
    gnani.transcript_script = [unsafe_transient(), Transcript("finally", 1.0, None)]
    assert h.step(job_id) == transcription.TRANSIENT_RETRY_SECONDS
    assert h.step(job_id) == transcription.SUMMARIZE_NOW and h.job(job_id).transcript == "finally"


def test_a_refused_transcript_download_is_not_mistaken_for_a_credentials_problem(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.transcript_script = [GnaniPermanentError("GET <external url> -> HTTP 403", status_code=403)]
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_FAILED  # not TRANSCRIPTION_AUTH_FAILED


def test_an_unexpected_gnani_response_shape_fails_the_job(h: Harness, gnani: FakeGnani) -> None:
    job_id = transcribing(h)
    gnani.job_script = [GnaniProtocolError("unexpected response shape")]
    assert h.step(job_id) is None
    assert h.job(job_id).error_code == failures.TRANSCRIPTION_FAILED


# --- what gets logged -----------------------------------------------------------------------------------------


def test_events_are_logged_with_ids_but_never_with_signed_urls(
    h: Harness, gnani: FakeGnani, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = queued_job(h.db)
    with caplog.at_level(logging.INFO):
        h.step(job_id)
        h.step(job_id)
    assert f"event=transcription_submitted job_id={job_id} gnani_job_id=gnani-job-1" in caplog.text
    assert f"event=transcript_saved job_id={job_id}" in caplog.text
    assert "storage.test" not in caplog.text and "s3.test" not in caplog.text and "secret" not in caplog.text


def test_the_transcript_link_is_used_at_once_and_never_stored(h: Harness, gnani: FakeGnani) -> None:
    job_id = queued_job(h.db)
    h.step(job_id)
    h.step(job_id)
    row: str = h.db.execute(
        text("SELECT to_jsonb(audio_jobs)::text FROM audio_jobs WHERE id = :i"), {"i": job_id}
    ).scalar_one()
    assert "s3.test" not in row and "sig=secret" not in row  # nothing of the expiring link reached the database
