import uuid

import pytest
from fakes import FakeQueue
from helpers import job_in_status
from sqlalchemy.orm import Session

from app.core.failures import Failure
from app.db.models import JobStatus
from app.services import jobs, recovery
from worker import tasks


def make_mixed_jobs(db: Session) -> set[uuid.UUID]:
    """One job per stage. Returns the ids of those that have a step in flight."""
    in_flight = {
        job_in_status(db, status)
        for status in (JobStatus.UPLOADED, JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING)
    }
    job_in_status(db, JobStatus.UPLOADING)  # still waiting for the browser
    failed = job_in_status(db, JobStatus.UPLOADED)
    jobs.fail_job(db, failed, Failure("X", "boom"))
    done = job_in_status(db, JobStatus.SUMMARIZING)
    jobs.transition(db, done, JobStatus.SUMMARIZING, JobStatus.COMPLETED)
    return in_flight


def test_only_jobs_with_a_step_in_flight_are_resumed(db: Session, queue: FakeQueue) -> None:
    expected = make_mixed_jobs(db)
    assert recovery.requeue_unfinished_jobs(db, queue) == 4  # a job mid-summary is in flight too
    assert {job_id for job_id, _countdown in queue.enqueued} == expected  # nothing finished, failed or still uploading


def test_nothing_to_resume_is_fine(db: Session, queue: FakeQueue) -> None:
    assert recovery.requeue_unfinished_jobs(db, queue) == 0 and queue.enqueued == []


def test_a_starting_worker_resumes_unfinished_jobs(
    monkeypatch: pytest.MonkeyPatch, db: Session, queue: FakeQueue
) -> None:
    expected = make_mixed_jobs(db)
    monkeypatch.setattr(tasks, "queue", queue)
    tasks.resume_unfinished_jobs()  # what Celery calls when the worker becomes ready
    assert {job_id for job_id, _countdown in queue.enqueued} == expected


def test_a_dead_queue_at_startup_does_not_stop_the_worker_from_starting(
    monkeypatch: pytest.MonkeyPatch, db: Session, queue: FakeQueue
) -> None:
    make_mixed_jobs(db)
    queue.outage = True
    monkeypatch.setattr(tasks, "queue", queue)
    tasks.resume_unfinished_jobs()  # logs the problem, must not raise
    assert queue.enqueued == []


def test_a_duplicate_resumed_step_is_harmless(db: Session, queue: FakeQueue) -> None:
    """The message a killed worker left behind and our fresh one may both run: the second finds it already claimed."""
    job_id = job_in_status(db, JobStatus.QUEUED)
    assert jobs.transition(db, job_id, JobStatus.QUEUED, JobStatus.TRANSCRIBING)  # first step claims it
    assert not jobs.transition(db, job_id, JobStatus.QUEUED, JobStatus.TRANSCRIBING)  # the duplicate does not
