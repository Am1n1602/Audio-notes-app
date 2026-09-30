import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import JobStatus
from app.db.session import get_sessionmaker
from app.services import jobs


def make_job(db: Session, name: str = "a.mp3") -> uuid.UUID:
    job_id = uuid.uuid4()
    jobs.create_job(
        db,
        job_id=job_id,
        original_filename=name,
        object_key=f"uploads/{job_id}/audio.mp3",
        mime_type="audio/mpeg",
        size_bytes=100,
        language_code="en-IN",
    )
    return job_id


def test_new_job_starts_uploading_with_timestamps(db: Session) -> None:
    job = jobs.get_job(db, make_job(db))
    assert job.status is JobStatus.UPLOADING
    assert job.created_at is not None and job.completed_at is None
    assert job.transcript is None and job.gnani_job_id is None


def test_unknown_job_is_a_404_app_error(db: Session) -> None:
    with pytest.raises(AppError) as info:
        jobs.get_job(db, uuid.uuid4())
    assert (info.value.status_code, info.value.code) == (404, "JOB_NOT_FOUND")


def test_list_is_newest_first_and_limited(db: Session) -> None:
    ids = [make_job(db, f"{n}.mp3") for n in range(3)]
    assert [j.id for j in jobs.list_jobs(db, 50)] == ids[::-1]
    assert [j.id for j in jobs.list_jobs(db, 2)] == ids[::-1][:2]


def test_transition_moves_the_job_once(db: Session) -> None:
    job_id = make_job(db)
    before = jobs.get_job(db, job_id).updated_at
    assert jobs.transition(db, job_id, JobStatus.UPLOADING, JobStatus.UPLOADED, progress_message="Uploaded") is True
    db.expire_all()
    job = jobs.get_job(db, job_id)
    assert (job.status, job.progress_message) == (JobStatus.UPLOADED, "Uploaded")
    assert job.updated_at > before
    # the same move again does nothing: the guard (status = UPLOADING) no longer matches
    assert jobs.transition(db, job_id, JobStatus.UPLOADING, JobStatus.UPLOADED) is False


def test_transition_from_the_wrong_status_changes_nothing(db: Session) -> None:
    job_id = make_job(db)
    assert jobs.transition(db, job_id, JobStatus.UPLOADED, JobStatus.QUEUED) is False
    db.expire_all()
    assert jobs.get_job(db, job_id).status is JobStatus.UPLOADING


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (JobStatus.UPLOADING, JobStatus.COMPLETED),  # skipping stages
        (JobStatus.UPLOADED, JobStatus.UPLOADING),  # going backwards
        (JobStatus.COMPLETED, JobStatus.FAILED),  # finished jobs stay finished
        (JobStatus.FAILED, JobStatus.QUEUED),  # retry is not allowed yet
    ],
)
def test_illegal_transitions_are_rejected_before_touching_the_database(
    db: Session, src: JobStatus, dst: JobStatus
) -> None:
    with pytest.raises(ValueError, match="illegal job transition"):
        jobs.transition(db, uuid.uuid4(), src, dst)


def test_every_processing_stage_can_fail_and_only_completion_sets_completed_at(db: Session) -> None:
    for stage in (
        JobStatus.UPLOADING,
        JobStatus.UPLOADED,
        JobStatus.QUEUED,
        JobStatus.TRANSCRIBING,
        JobStatus.SUMMARIZING,
    ):
        assert JobStatus.FAILED in jobs.ALLOWED_TRANSITIONS[stage]
    job_id = make_job(db)
    jobs.transition(db, job_id, JobStatus.UPLOADING, JobStatus.FAILED, error_code="X", error_message="boom")
    db.expire_all()
    failed = jobs.get_job(db, job_id)
    assert (failed.status, failed.error_code, failed.error_message, failed.completed_at) == (
        JobStatus.FAILED,
        "X",
        "boom",
        None,
    )


def test_full_happy_path_reaches_completed_with_a_completed_at(db: Session) -> None:
    job_id = make_job(db)
    path = [
        JobStatus.UPLOADING,
        JobStatus.UPLOADED,
        JobStatus.QUEUED,
        JobStatus.TRANSCRIBING,
        JobStatus.SUMMARIZING,
    ]
    for src, dst in zip(path, path[1:] + [JobStatus.COMPLETED], strict=True):
        assert jobs.transition(db, job_id, src, dst) is True
    db.expire_all()
    done = jobs.get_job(db, job_id)
    assert done.status is JobStatus.COMPLETED and done.completed_at is not None


def test_concurrent_transitions_have_exactly_one_winner(db: Session) -> None:
    """The property Phase 3 relies on: many callers race, one gets True, so one enqueues the work."""
    job_id = make_job(db)

    def attempt(_: int) -> bool:
        with get_sessionmaker()() as session:  # each caller has its own connection, like separate requests
            return jobs.transition(session, job_id, JobStatus.UPLOADING, JobStatus.UPLOADED)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(True) == 1 and results.count(False) == 7
