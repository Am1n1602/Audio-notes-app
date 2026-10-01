"""One chain of steps per job, and steps that do not hold resources they should not.

A job's steps reschedule themselves, so a second start of the worker, a second replica or a redelivered message would
otherwise leave two chains running until the job ends, each spending its own LLM and Gnani calls.
"""

import time
import uuid
from typing import Any

import pytest
from fakes import FakeGnani, FakeLlm, FakeQueue, FakeStorage
from helpers import job_in_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings, get_settings
from app.db.models import AudioJob, JobStatus
from app.providers.llm import LlmTransientError, LlmTruncatedError
from app.providers.queue import QueueError, make_celery_app
from app.services import jobs, pipeline, recovery, steps, summarization
from app.services.summary_prompt import get_prompts
from worker import tasks

SETTINGS = get_settings()
SMALL = SETTINGS.model_copy(update={"llm_chunk_chars": 100, "llm_chunk_interval_seconds": 15})
LONG = " ".join(f"word{n:02d}" for n in range(40))  # 3 parts of at most 100 characters


def summarizing_job(db: Session, transcript: str = LONG) -> uuid.UUID:
    job_id = job_in_status(db, JobStatus.SUMMARIZING)
    db.execute(text("UPDATE audio_jobs SET transcript = :t WHERE id = :i"), {"t": transcript, "i": job_id})
    db.commit()
    return job_id


def run_step(db: Session, llm: FakeLlm, job_id: uuid.UUID, settings: Settings = SMALL) -> int | None:
    db.expire_all()
    return pipeline.process_job(db, FakeGnani(), FakeStorage(), llm, settings, job_id, sleep=lambda _s: None)


def seconds_until_due(db: Session, job_id: uuid.UUID) -> float | None:
    db.expire_all()
    return db.execute(
        text("SELECT extract(epoch FROM next_step_at - now()) FROM audio_jobs WHERE id = :i"), {"i": job_id}
    ).scalar_one()


# --- is_step_due ------------------------------------------------------------------------------------------------


def test_a_job_with_no_recorded_due_time_is_due_now(db: Session) -> None:
    assert jobs.is_step_due(db, summarizing_job(db)) is True


@pytest.mark.parametrize(("seconds_ahead", "due"), [(-30, True), (0, True), (3, True), (10, False), (300, False)])
def test_a_step_is_due_unless_it_is_well_ahead_of_schedule(db: Session, seconds_ahead: int, due: bool) -> None:
    job_id = summarizing_job(db)
    db.execute(
        text("UPDATE audio_jobs SET next_step_at = now() + make_interval(secs => :s) WHERE id = :i"),
        {"s": seconds_ahead, "i": job_id},
    )
    db.commit()
    assert jobs.is_step_due(db, job_id) is due  # 3 s early is tolerated (clock and delivery jitter), 10 s is not


def test_an_unknown_job_is_not_due(db: Session) -> None:
    assert jobs.is_step_due(db, uuid.uuid4()) is False


def test_set_and_clear_next_step(db: Session) -> None:
    job_id = summarizing_job(db)
    jobs.set_next_step(db, job_id, 30)
    assert 25 < (seconds_until_due(db, job_id) or 0) <= 30 and jobs.is_step_due(db, job_id) is False
    jobs.clear_next_step(db, job_id)
    assert seconds_until_due(db, job_id) is None and jobs.is_step_due(db, job_id) is True


@pytest.mark.parametrize("resume_summary", [False, True])
def test_a_retry_starts_a_fresh_chain(db: Session, resume_summary: bool) -> None:
    job_id = summarizing_job(db)
    jobs.fail_job(db, job_id, failures.Failure(failures.SUMMARY_UNAVAILABLE, "x"))
    jobs.set_next_step(db, job_id, 300)  # a stale marker from the chain that died
    assert jobs.requeue_failed(db, job_id, resume_summary=resume_summary) is True
    assert seconds_until_due(db, job_id) is None


# --- the pipeline drops a duplicate step --------------------------------------------------------------------------


def test_a_step_that_arrives_early_is_a_duplicate_and_does_nothing(db: Session) -> None:
    job_id = summarizing_job(db)
    llm = FakeLlm()
    jobs.set_next_step(db, job_id, 30)  # the other chain's next step is due in 30 s
    assert run_step(db, llm, job_id) is None  # ends: it must not reschedule itself
    assert llm.calls == []  # and it must not spend a call


def test_the_step_that_is_on_time_runs(db: Session) -> None:
    job_id = summarizing_job(db)
    llm = FakeLlm()
    jobs.set_next_step(db, job_id, 0)
    assert run_step(db, llm, job_id) == 15 and len(llm.calls) == 1


# --- through the Celery task: two chains become one ------------------------------------------------------------------


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, db: Session) -> tuple[FakeQueue, FakeLlm]:
    queue, llm = FakeQueue(), FakeLlm()
    monkeypatch.setattr(tasks, "queue", queue)
    monkeypatch.setattr(steps, "get_llm", lambda: llm)  # the step runner both entry points share looks these up
    monkeypatch.setattr(steps, "get_gnani", FakeGnani)
    monkeypatch.setattr(steps, "get_storage", FakeStorage)
    monkeypatch.setattr(steps, "get_settings", lambda: SMALL)
    return queue, llm


def test_the_task_records_when_the_next_step_is_due_before_it_enqueues_it(
    db: Session, worker: tuple[FakeQueue, FakeLlm]
) -> None:
    queue, _llm = worker
    job_id = summarizing_job(db)
    assert tasks.process_job.apply(args=[str(job_id)]).successful()
    assert queue.enqueued == [(job_id, 15)]
    assert 10 < (seconds_until_due(db, job_id) or 0) <= 15


def test_a_failed_enqueue_leaves_no_due_time_behind_so_the_retries_can_run(
    db: Session, worker: tuple[FakeQueue, FakeLlm]
) -> None:
    """If the enqueue fails, nothing is scheduled: the Celery retry IS the next step. It must not find a due time in
    the future and mistake itself for a duplicate."""
    queue, llm = worker
    job_id = summarizing_job(db, " ".join(f"word{n:03d}" for n in range(240)))  # 20 parts: never done in 11 steps
    queue.outage = True
    result = tasks.process_job.apply(args=[str(job_id)])
    assert result.failed() and isinstance(result.result, QueueError)  # gave up after its 10 bounded retries
    assert len(llm.calls) == 11  # the first run and all ten retries did real work: none was dropped as a duplicate
    assert seconds_until_due(db, job_id) is None


def test_two_chains_for_one_job_become_one(db: Session, worker: tuple[FakeQueue, FakeLlm]) -> None:
    """Seen live: after a restart, a recovery step and a stale message both ran, and parts were saved 2 s apart."""
    queue, llm = worker
    job_id = summarizing_job(db)

    assert tasks.process_job.apply(args=[str(job_id)]).successful()  # chain A: part 1, schedules part 2 in 15 s
    assert tasks.process_job.apply(args=[str(job_id)]).successful()  # chain B arrives right after: a duplicate
    assert len(llm.calls) == 1 and queue.enqueued == [(job_id, 15)]  # no second call, and B did not reschedule

    db.execute(text("UPDATE audio_jobs SET next_step_at = now() - interval '1 second' WHERE id = :i"), {"i": job_id})
    db.commit()
    assert tasks.process_job.apply(args=[str(job_id)]).successful()  # chain A's own step, on time
    assert len(llm.calls) == 2 and queue.enqueued == [(job_id, 15), (job_id, 15)]
    db.expire_all()
    assert len(jobs.get_job(db, job_id).summary_partials or []) == 2


def test_a_worker_start_begins_a_fresh_chain_even_though_the_old_one_left_a_due_time(
    db: Session, worker: tuple[FakeQueue, FakeLlm]
) -> None:
    queue, llm = worker
    job_id = summarizing_job(db)
    jobs.set_next_step(db, job_id, 300)  # the killed worker's step was due in 5 minutes
    assert recovery.requeue_unfinished_jobs(db, queue) == 1
    assert seconds_until_due(db, job_id) is None and queue.enqueued == [(job_id, 0)]
    assert tasks.process_job.apply(args=[str(job_id)]).successful()  # the recovery step is not taken for a duplicate
    assert len(llm.calls) == 1


class ClashingLlm(FakeLlm):
    """While this step waits for the model, a duplicate step saves the same part first."""

    def __init__(self, other_chain: Any) -> None:
        super().__init__()
        self.other_chain = other_chain

    def complete_json(self, system: str, user: str) -> Any:
        self.other_chain()
        return super().complete_json(system, user)


def test_a_duplicate_step_that_loses_the_save_ends_its_chain(db: Session) -> None:
    job_id = summarizing_job(db)
    other = [{"covers": 1, "chunk_chars": 100, "summary": {"overview": "saved by the other chain"}}]
    llm = ClashingLlm(lambda: jobs.save_summary_partials(db, job_id, 0, other))
    assert run_step(db, llm, job_id) is None  # not 15: this chain must not go on to reschedule itself
    db.expire_all()
    assert jobs.get_job(db, job_id).summary_partials == other  # the other chain's part stands, saved once


# --- no transaction held open across network calls -------------------------------------------------------------------


class WatchingLlm(FakeLlm):
    def __init__(self, db: Session) -> None:
        super().__init__()
        self.db, self.in_transaction = db, []  # type: ignore[var-annotated]

    def complete_json(self, system: str, user: str) -> Any:
        self.in_transaction.append(self.db.in_transaction())
        return super().complete_json(system, user)


class WatchingGnani(FakeGnani):
    def __init__(self, db: Session) -> None:
        super().__init__()
        self.db, self.in_transaction = db, []  # type: ignore[var-annotated]

    def get_batch_job(self, job_id: str) -> Any:
        self.in_transaction.append(self.db.in_transaction())
        return super().get_batch_job(job_id)


def test_the_database_transaction_is_closed_while_waiting_for_the_llm(db: Session) -> None:
    """A connection left 'idle in transaction' through a slow model call is killed by managed Postgres."""
    job_id = summarizing_job(db, "just a few words")
    llm = WatchingLlm(db)
    db.expire_all()
    pipeline.process_job(db, FakeGnani(), FakeStorage(), llm, SETTINGS, job_id, sleep=lambda _s: None)
    assert llm.in_transaction == [False]


def test_the_database_transaction_is_closed_while_waiting_for_gnani(db: Session) -> None:
    job_id = job_in_status(db, JobStatus.TRANSCRIBING)
    db.execute(
        text("UPDATE audio_jobs SET gnani_job_id = 'g-1', submit_started_at = now() WHERE id = :i"), {"i": job_id}
    )
    db.commit()
    gnani = WatchingGnani(db)
    db.expire_all()
    pipeline.process_job(db, gnani, FakeStorage(), FakeLlm(), SETTINGS, job_id, sleep=lambda _s: None)
    assert gnani.in_transaction == [False]


# --- a step cannot outlive Redis' visibility timeout -----------------------------------------------------------------


def test_the_worst_case_length_of_a_summary_step_is_inside_the_visibility_timeout() -> None:
    from app.providers.llm import GroqClient

    visibility = make_celery_app(SETTINGS).conf.broker_transport_options["visibility_timeout"]
    timeout = GroqClient("k", "https://llm.test", "m", 900)._http.timeout
    last_attempt = (timeout.connect or 0) + (timeout.read or 0)  # the last attempt may start just inside the budget
    assert summarization.STEP_BUDGET_SECONDS + last_attempt < visibility


class SlowFailingLlm(FakeLlm):
    """Every call fails after 100 seconds (a stalled connection)."""

    def __init__(self, advance_clock: Any) -> None:
        super().__init__()
        self.advance_clock = advance_clock

    def complete_json(self, system: str, user: str) -> Any:
        self.calls.append((system, user))
        self.advance_clock(100)
        raise LlmTransientError("POST /chat/completions: ReadTimeout")


def test_no_retry_starts_once_the_step_budget_is_spent(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])
    job_id = summarizing_job(db, "a few words")
    llm = SlowFailingLlm(lambda seconds: offset.__setitem__(0, offset[0] + seconds))
    sleeps: list[float] = []

    db.expire_all()
    job = jobs.get_job(db, job_id)
    summarization.step(db, llm, SMALL, job, sleep=sleeps.append)

    # call 1 ends at 100 s; waiting 2 s is inside the 180 s budget, so call 2 runs and ends at 200 s; waiting 4 s
    # more would pass the budget, so the step gives up (rather than run all five attempts, about 500 s)
    assert len(llm.calls) == 2 and sleeps == [2]
    db.expire_all()
    assert jobs.get_job(db, job_id).error_code == failures.SUMMARY_UNAVAILABLE


# --- a cut-off answer is retried with a request to be shorter ---------------------------------------------------------


def one_call_job(db: Session) -> tuple[uuid.UUID, AudioJob]:
    job_id = summarizing_job(db, "a few words")
    db.expire_all()
    return job_id, jobs.get_job(db, job_id)


def test_after_a_cut_off_answer_the_retry_asks_for_a_much_shorter_one(db: Session) -> None:
    job_id, job = one_call_job(db)
    llm = FakeLlm()
    llm.script = [LlmTruncatedError("cut off at the token limit"), FakeLlm.GOOD]
    summarization.step(db, llm, SETTINGS, job, sleep=lambda _s: None)
    first, second = llm.calls[0][1], llm.calls[1][1]
    assert get_prompts().shorter_note not in first
    assert second == first + "\n\n" + get_prompts().shorter_note  # only the user message grows; system is untouched
    assert llm.calls[0][0] == llm.calls[1][0]
    db.expire_all()
    assert jobs.get_job(db, job_id).status is JobStatus.COMPLETED


def test_a_badly_formed_answer_is_retried_unchanged(db: Session) -> None:
    """Only a CUT-OFF answer needs a shorter request; bad JSON is just asked for again."""
    _job_id, job = one_call_job(db)
    llm = FakeLlm()
    llm.script = ["this is not json", FakeLlm.GOOD]
    summarization.step(db, llm, SETTINGS, job, sleep=lambda _s: None)
    assert llm.calls[0] == llm.calls[1]


def test_two_cut_off_answers_in_a_row_fail_visibly_with_the_transcript_kept(db: Session) -> None:
    job_id, job = one_call_job(db)
    llm = FakeLlm()
    llm.script = [LlmTruncatedError("cut off")]
    summarization.step(db, llm, SETTINGS, job, sleep=lambda _s: None)
    assert len(llm.calls) == 2
    db.expire_all()
    failed = jobs.get_job(db, job_id)
    assert (failed.status, failed.error_code) == (JobStatus.FAILED, failures.SUMMARY_INVALID_RESPONSE)
    assert failed.transcript == "a few words"
