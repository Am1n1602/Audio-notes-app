"""A job whose chain of steps broke (a database outage outlasting the worker's retries, a lost message) must not sit 'in
progress' for ever: when its owner's page asks about it, it is given a new step."""

import logging
import uuid

import pytest
from fakes import FakeQueue
from fastapi.testclient import TestClient
from helpers import job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.failures import Failure
from app.db.models import JobStatus
from app.services import jobs, recovery

AFTER = get_settings().job_stalled_after_seconds


def silent_for(db: Session, job_id: uuid.UUID, seconds: int) -> None:
    """Make the job look like nothing has happened to it for this long (an explicit updated_at wins over onupdate)."""
    db.execute(
        text("UPDATE audio_jobs SET updated_at = now() - make_interval(secs => :s) WHERE id = :i"),
        {"s": seconds, "i": job_id},
    )
    db.commit()


@pytest.mark.parametrize(
    "status", [JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING]
)
def test_asking_about_a_job_whose_steps_stopped_gives_it_a_new_step(
    api: TestClient, db: Session, queue: FakeQueue, status: JobStatus
) -> None:
    job_id = job_in_status(db, status)
    silent_for(db, job_id, AFTER + 60)
    assert api.get(f"/api/uploads/{job_id}").status_code == 200
    assert queue.enqueued == [(job_id, 0)]


def test_a_healthy_job_is_left_alone(api: TestClient, db: Session, queue: FakeQueue) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    silent_for(db, job_id, AFTER - 60)  # quiet for a while, but within what an honest step can take
    api.get(f"/api/uploads/{job_id}")
    assert queue.enqueued == []


def finished_job(db: Session, status: JobStatus) -> uuid.UUID:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    if status is JobStatus.COMPLETED:
        assert jobs.transition(db, job_id, JobStatus.SUMMARIZING, JobStatus.COMPLETED)
    else:
        assert jobs.fail_job(db, job_id, Failure("X", "x"))
    return job_id


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED])
def test_a_finished_job_is_never_revived(api: TestClient, db: Session, queue: FakeQueue, status: JobStatus) -> None:
    job_id = finished_job(db, status)
    silent_for(db, job_id, AFTER * 10)
    api.get(f"/api/uploads/{job_id}")
    assert queue.enqueued == []


def test_a_job_still_waiting_for_the_browser_is_never_revived(api: TestClient, db: Session, queue: FakeQueue) -> None:
    job_id = job_in_status(db, JobStatus.UPLOADING)
    silent_for(db, job_id, AFTER * 10)
    api.get(f"/api/uploads/{job_id}")
    assert queue.enqueued == []


def test_a_job_is_revived_once_not_on_every_poll(api: TestClient, db: Session, queue: FakeQueue) -> None:
    """The page asks every few seconds. The claim restarts the quiet period, so only the first ask revives."""
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    silent_for(db, job_id, AFTER + 60)
    for _ in range(4):
        api.get(f"/api/uploads/{job_id}")
    assert len(queue.enqueued) == 1


def test_two_pages_asking_at_once_revive_it_once(db: Session) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    silent_for(db, job_id, AFTER + 60)
    assert jobs.claim_stalled(db, [job_id], AFTER) == [job_id]
    assert jobs.claim_stalled(db, [job_id], AFTER) == []  # the second asker finds it already claimed


def test_reviving_clears_the_due_time_so_the_new_step_is_not_taken_for_a_duplicate(db: Session) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    db.execute(text("UPDATE audio_jobs SET next_step_at = now() + interval '1 hour' WHERE id = :i"), {"i": job_id})
    db.commit()
    silent_for(db, job_id, AFTER + 60)
    jobs.claim_stalled(db, [job_id], AFTER)
    db.expire_all()
    assert jobs.get_job(db, job_id).next_step_at is None and jobs.is_step_due(db, job_id)


def test_someone_elses_stalled_job_is_neither_shown_nor_revived(
    api: TestClient, api_b: TestClient, db: Session, queue: FakeQueue
) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)  # browser A's job
    silent_for(db, job_id, AFTER + 60)
    assert api_b.get(f"/api/uploads/{job_id}").status_code == 404
    assert api_b.get("/api/uploads").json() == [] and queue.enqueued == []


def test_the_history_list_revives_the_owners_stalled_jobs_too(api: TestClient, db: Session, queue: FakeQueue) -> None:
    stuck = job_in_status(db, JobStatus.SUMMARIZING)
    fine = job_in_status(db, JobStatus.QUEUED)
    silent_for(db, stuck, AFTER + 60)
    db.execute(text("UPDATE audio_jobs SET updated_at = now() WHERE id = :i"), {"i": fine})
    db.commit()
    assert len(api.get("/api/uploads").json()) == 2
    assert queue.enqueued == [(stuck, 0)]


def test_a_dead_broker_does_not_break_reading_a_job(
    api: TestClient, db: Session, queue: FakeQueue, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    silent_for(db, job_id, AFTER + 60)
    queue.outage = True
    res = api.get(f"/api/uploads/{job_id}")
    assert res.status_code == 200 and res.json()["status"] == "TRANSCRIBING"  # still readable
    assert "job_revive_failed" in caplog.text


def test_looks_stalled_needs_an_active_job_and_a_long_silence(db: Session) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    job = jobs.get_job(db, job_id)
    assert not recovery.looks_stalled(job, AFTER)
    silent_for(db, job_id, AFTER + 60)
    db.expire_all()
    assert recovery.looks_stalled(jobs.get_job(db, job_id), AFTER)


@pytest.mark.parametrize("status", [JobStatus.UPLOADING, JobStatus.COMPLETED, JobStatus.FAILED])
def test_the_claim_itself_refuses_a_job_that_is_not_waiting_for_a_step(db: Session, status: JobStatus) -> None:
    """A job can finish between the cheap look and the claim, so the claim itself must refuse it."""
    job_id = job_in_status(db, status) if status is JobStatus.UPLOADING else finished_job(db, status)
    silent_for(db, job_id, AFTER * 10)
    assert jobs.claim_stalled(db, [job_id], AFTER) == []
