import json
import uuid
from typing import Any

import pytest
from fakes import FakeGnani, FakeLlm, FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import get_settings
from app.db.models import JobStatus
from app.services import jobs, pipeline

SETTINGS = get_settings()


def run(db: Session, gnani: FakeGnani, llm: FakeLlm, job_id: uuid.UUID) -> int | None:
    db.expire_all()
    return pipeline.process_job(db, gnani, FakeStorage(), llm, SETTINGS, job_id, sleep=lambda _s: None)


def test_a_queued_job_runs_the_whole_chain_from_upload_to_a_summary(db: Session) -> None:
    gnani, llm = FakeGnani(), FakeLlm()
    job_id = job_in_status(db, JobStatus.QUEUED)
    steps = []
    while (delay := run(db, gnani, llm, job_id)) is not None:
        steps.append(delay)
        assert len(steps) < 10  # the chain must end
    assert steps == [10, 0]  # poll in 10 s; then the transcript is saved and the summary runs at once
    db.expire_all()
    job = jobs.get_job(db, job_id)
    assert job.status is JobStatus.COMPLETED
    assert job.transcript == "hello world" and (job.summary or {})["overview"] == "A planning meeting."
    assert gnani.calls.count("create") == 1 and len(llm.calls) == 1  # one Gnani job, one LLM call


def test_a_job_in_summarizing_never_touches_gnani(db: Session) -> None:
    gnani, llm = FakeGnani(), FakeLlm()
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(text("UPDATE audio_jobs SET transcript = 'some words'"))
    db.commit()
    assert run(db, gnani, llm, job_id) is None
    assert gnani.calls == [] and len(llm.calls) == 1


def test_finished_jobs_are_left_alone(db: Session) -> None:
    gnani, llm = FakeGnani(), FakeLlm()
    done = job_in_status(db, JobStatus.SUMMARIZING)
    jobs.transition(db, done, JobStatus.SUMMARIZING, JobStatus.COMPLETED)
    assert run(db, gnani, llm, done) is None and gnani.calls == [] and llm.calls == []


def test_a_duplicate_delivery_after_completion_changes_nothing(db: Session) -> None:
    gnani, llm = FakeGnani(), FakeLlm()
    job_id = job_in_status(db, JobStatus.QUEUED)
    while run(db, gnani, llm, job_id) is not None:
        pass
    calls_before = (list(gnani.calls), len(llm.calls))
    assert run(db, gnani, llm, job_id) is None  # e.g. the message a killed worker left behind
    assert (list(gnani.calls), len(llm.calls)) == calls_before  # no new Gnani or LLM work, no extra cost


# --- a failed summary keeps the transcript, and Retry does not re-transcribe -------------------------------------


def failed_after_transcript(db: Session, code: str) -> str:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(
        text("UPDATE audio_jobs SET transcript = 'the saved transcript', gnani_job_id = 'g-1' WHERE id = :i"),
        {"i": job_id},
    )
    db.commit()
    assert jobs.fail_job(db, job_id, failures.Failure(code, "The summary failed. The transcript is ready."))
    return str(job_id)


def body(res: Any) -> dict[str, Any]:
    return res.json()  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "code", [failures.SUMMARY_UNAVAILABLE, failures.SUMMARY_RATE_LIMITED, failures.SUMMARY_INVALID_RESPONSE]
)
def test_retry_after_a_summary_failure_resumes_at_the_summary_and_keeps_the_transcript(
    api: TestClient, db: Session, queue: FakeQueue, code: str
) -> None:
    job_id = failed_after_transcript(db, code)
    detail = body(api.get(f"/api/uploads/{job_id}"))
    assert (detail["status"], detail["transcript"], detail["can_retry"]) == ("FAILED", "the saved transcript", True)

    retried = body(api.post(f"/api/uploads/{job_id}/retry"))
    assert retried["status"] == "SUMMARIZING" and retried["error_code"] is None  # NOT queued for transcription
    assert retried["transcript"] == "the saved transcript"
    assert len(queue.enqueued) == 1
    db.expire_all()
    assert jobs.get_job(db, uuid.UUID(job_id)).gnani_job_id == "g-1"  # the Gnani job is untouched: no re-transcription


@pytest.mark.parametrize("code", [failures.INTERNAL_ERROR, failures.QUEUE_UNAVAILABLE])
def test_any_retryable_failure_with_a_saved_transcript_resumes_at_the_summary(
    api: TestClient, db: Session, queue: FakeQueue, code: str
) -> None:
    """A crash records INTERNAL_ERROR and a dead queue records QUEUE_UNAVAILABLE. Neither is a summary code, but the
    transcript is saved, so Retry must not transcribe (and pay for) the recording again."""
    job_id = failed_after_transcript(db, code)
    db.execute(
        text("UPDATE audio_jobs SET summary_partials = CAST(:p AS jsonb) WHERE id = :i"), {"p": "[]", "i": job_id}
    )
    db.commit()
    retried = body(api.post(f"/api/uploads/{job_id}/retry"))
    assert (retried["status"], retried["transcript"]) == ("SUMMARIZING", "the saved transcript")
    db.expire_all()
    assert jobs.get_job(db, uuid.UUID(job_id)).gnani_job_id == "g-1"  # the Gnani job is untouched
    assert len(queue.enqueued) == 1


def test_a_retry_that_hits_a_dead_queue_does_not_lose_the_resume_point(
    api: TestClient, db: Session, queue: FakeQueue
) -> None:
    """Retry after a summary failure while Redis is down overwrites the error code with QUEUE_UNAVAILABLE. The second
    Retry must still resume at the summary and keep the finished parts, not re-transcribe."""
    job_id = failed_after_transcript(db, failures.SUMMARY_UNAVAILABLE)
    saved = [{"covers": 1, "chunk_chars": 100, "summary": {"overview": "part one"}}]
    db.execute(
        text("UPDATE audio_jobs SET summary_partials = CAST(:p AS jsonb) WHERE id = :i"),
        {"p": json.dumps(saved), "i": job_id},
    )
    db.commit()

    queue.outage = True
    assert api.post(f"/api/uploads/{job_id}/retry").status_code == 503
    assert body(api.get(f"/api/uploads/{job_id}"))["error_code"] == failures.QUEUE_UNAVAILABLE

    queue.outage = False
    retried = body(api.post(f"/api/uploads/{job_id}/retry"))
    assert (retried["status"], retried["transcript"]) == ("SUMMARIZING", "the saved transcript")
    db.expire_all()
    job = jobs.get_job(db, uuid.UUID(job_id))
    assert job.gnani_job_id == "g-1" and job.summary_partials == saved


@pytest.mark.parametrize("code", [failures.SUMMARY_AUTH_FAILED, failures.SUMMARY_REQUEST_REJECTED])
def test_summary_failures_that_retrying_cannot_fix_are_refused(
    api: TestClient, db: Session, queue: FakeQueue, code: str
) -> None:
    job_id = failed_after_transcript(db, code)
    assert body(api.get(f"/api/uploads/{job_id}"))["can_retry"] is False
    res = api.post(f"/api/uploads/{job_id}/retry")
    assert (res.status_code, body(res)["error"]["code"]) == (409, "NOT_RETRYABLE") and queue.enqueued == []


def test_the_detail_shows_the_transcript_alongside_a_summary_failure(api: TestClient, db: Session) -> None:
    job_id = failed_after_transcript(db, failures.SUMMARY_UNAVAILABLE)
    detail = body(api.get(f"/api/uploads/{job_id}"))
    assert detail["transcript"] == "the saved transcript" and detail["summary"] is None
    assert "transcript is ready" in detail["error_message"]


def test_a_transcription_failure_still_retries_from_the_start(api: TestClient, db: Session, queue: FakeQueue) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADED)
    jobs.fail_job(db, job_id, failures.Failure(failures.TRANSCRIPTION_UNAVAILABLE, "Gnani was busy."))
    retried = body(api.post(f"/api/uploads/{job_id}/retry"))
    assert retried["status"] == "QUEUED" and len(queue.enqueued) == 1


def test_a_completed_summary_is_returned_in_the_api_response(api: TestClient, db: Session) -> None:
    gnani, llm = FakeGnani(), FakeLlm()
    job_id = job_in_status(db, JobStatus.QUEUED)
    while run(db, gnani, llm, job_id) is not None:
        pass
    detail = body(api.get(f"/api/uploads/{job_id}"))
    assert detail["status"] == "COMPLETED" and detail["progress_message"] is None
    assert set(detail["summary"]) == {"overview", "key_points", "action_items", "decisions", "uncertainties"}
