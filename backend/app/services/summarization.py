"""Turns a saved transcript into a structured, grounded summary, one bounded step at a time.

summarization.step() does ONE unit of work and returns how many seconds until the next step is due (or None when done),
exactly like the transcription steps. A short transcript needs one LLM call. A long one is split into parts (Groq caps
tokens per minute, and a 4-hour recording is far more than a model should read in one go): each
step summarises ONE part and saves it, the next step does the next part, and final steps merge the part-summaries.
A single request has a size cap too, so at most MERGE_FAN_IN summaries are merged per call: a 4-hour recording (dozens
of parts) is folded down a few at a time until few enough remain for the final merge.
Every step re-reads the job from Postgres, so a crash or a duplicate delivery never repeats work that was already saved.

Whatever the model returns is untrusted: it is parsed and validated against the Summary schema (bounded lengths, only
the five required fields) before anything is stored.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from math import ceil
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core import failures
from app.core.config import Settings
from app.core.failures import Failure
from app.core.logging import log_event
from app.db.models import AudioJob, JobStatus
from app.providers.llm import (
    LlmError,
    LlmPermanentError,
    LlmProtocolError,
    LlmTransientError,
    LlmTruncatedError,
    SummaryLlm,
)
from app.schemas.summary import Summary
from app.services import jobs
from app.services.summary_prompt import SummaryPrompts, get_prompts

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # LLM calls per unit of work, counting the first (the same policy as Gnani's Create/Start)
# One step must finish well inside Redis' visibility timeout (300 s, providers/queue.py): a step that is still running
# when that expires is delivered a second time and the paid calls run twice. The LLM client's timeouts are 5 s to
# connect and 30 s to read, so no retry starts after this budget and the worst case is about 180 + 35 seconds.
STEP_BUDGET_SECONDS = 180
BACKOFF_SECONDS = (2, 4, 8, 16)  # pauses between attempts when the LLM gave no retry-after
MERGE_FAN_IN = 8  # part-summaries merged per call: 8 x ~500 tokens keeps a merge well inside a small per-minute cap

Sleep = Callable[[float], None]
NextStep = int | None


def split_transcript(text: str, max_chars: int) -> list[str]:
    """Split on word boundaries into parts of at most max_chars (a single longer word gets a part to itself).
    Deterministic, so a step that restarts computes the same parts and resumes at the right one."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for word in text.split():
        if current and size + len(word) + 1 > max_chars:
            parts.append(" ".join(current))
            current, size = [], 0
        current.append(word)
        size += len(word) + 1
    if current:
        parts.append(" ".join(current))
    return parts


def parse_summary(content: str) -> Summary:
    """The model's raw text -> a validated Summary, or LlmProtocolError. Never trust, always validate."""
    text = content.strip()
    if text.startswith("```"):  # some models fence their JSON even when told not to
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LlmProtocolError("the summary was not valid JSON") from exc
    try:
        return Summary.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(p) for p in error["loc"]) or "?" for error in exc.errors()}))
        raise LlmProtocolError(f"the summary does not match the required structure (fields: {fields})") from exc


def step(db: Session, llm: SummaryLlm, settings: Settings, job: AudioJob, sleep: Sleep = time.sleep) -> NextStep:
    """One unit of summarising work for a job in SUMMARIZING."""
    transcript = (job.transcript or "").strip()
    if not transcript:  # defensive: the transcription step never saves a blank transcript
        return _fail(db, job, Failure(failures.EMPTY_TRANSCRIPT, "There is no transcript to summarize."), None)

    prompts = get_prompts()
    items: list[dict[str, Any]] = job.summary_partials or []  # each: {"covers", "chunk_chars", "summary"}
    # A job that has started keeps the part size it started with. Re-splitting at a new size after a config change or
    # a deploy would cut the transcript at different places and silently skip or repeat text.
    chunk_chars = items[0]["chunk_chars"] if items else settings.llm_chunk_chars
    parts = split_transcript(transcript, chunk_chars)
    done = sum(item["covers"] for item in items)  # parts already summarised, however far they have been folded down
    try:
        if len(parts) == 1:
            summary = _ask(llm, settings, prompts.system, prompts.user_message(parts[0]), sleep, job, "summary")
            return _complete(db, job, summary)
        if done < len(parts):
            partial = _ask(
                llm,
                settings,
                prompts.system,
                prompts.part_message(parts[done], done + 1, len(parts)),
                sleep,
                job,
                "part",
            )
            item = {"covers": 1, "chunk_chars": chunk_chars, "summary": partial.model_dump()}
            if not jobs.save_summary_partials(db, job.id, len(items), [*items, item]):
                return None  # a duplicate step saved this part first: that chain carries on, this one ends here
            log_event(logger, "summary_part_saved", job_id=job.id, part=done + 1, total=len(parts))
            return settings.llm_chunk_interval_seconds  # keep under the tokens-per-minute cap; next step re-reads
        if len(items) > MERGE_FAN_IN:  # too many for one request: fold the first few into one, come back for more
            group = items[:MERGE_FAN_IN]
            folded = _ask(llm, settings, prompts.merge_system, _merge_input(group, prompts), sleep, job, "fold")
            covered = sum(g["covers"] for g in group)
            item = {"covers": covered, "chunk_chars": chunk_chars, "summary": folded.model_dump()}
            if not jobs.save_summary_partials(db, job.id, len(items), [item, *items[MERGE_FAN_IN:]]):
                return None  # a duplicate step folded them first
            remaining = len(items) - len(group) + 1
            log_event(logger, "summary_parts_folded", job_id=job.id, folded=len(group), remaining=remaining)
            return settings.llm_chunk_interval_seconds
        summary = _ask(llm, settings, prompts.merge_system, _merge_input(items, prompts), sleep, job, "merge")
        return _complete(db, job, summary)
    except LlmError as exc:
        return _fail(db, job, failure_from_error(exc, settings), exc)


def _merge_input(items: list[dict[str, Any]], prompts: SummaryPrompts) -> str:
    return prompts.merge_message(json.dumps([item["summary"] for item in items], ensure_ascii=False))


def _ask(
    llm: SummaryLlm, settings: Settings, system: str, user: str, sleep: Sleep, job: AudioJob, what: str
) -> Summary:
    """One validated summary from the LLM, with bounded retries:
    - a transient error waits for the LLM's own retry-after (or 2, 4, 8, 16 s) and tries again;
    - a wait longer than settings.llm_max_wait_seconds is not worth blocking a worker for, so it propagates and the
      job fails as rate limited (the user can retry later, and the transcript is kept). The same goes for a wait that
      would take the whole step past STEP_BUDGET_SECONDS;
    - an unusable answer (bad JSON, wrong structure) gets exactly one more try, and an answer that was cut off gets
      that try with a note asking for a much shorter answer (asking again for the same thing is cut off again);
    - anything else propagates at once.
    """
    bad_answers = 0
    shorter = False
    step_started = time.monotonic()
    for attempt in range(MAX_ATTEMPTS):
        started = time.monotonic()
        message = user + "\n\n" + get_prompts().shorter_note if shorter else user
        try:
            result = llm.complete_json(system, message)
            summary = parse_summary(result.content)
        except LlmTransientError as exc:
            wait = (
                exc.retry_after
                if exc.retry_after is not None
                else BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            )
            over_budget = time.monotonic() - step_started + wait > STEP_BUDGET_SECONDS
            if attempt == MAX_ATTEMPTS - 1 or wait > settings.llm_max_wait_seconds or over_budget:
                raise
            log_event(logger, "llm_call_retry", job_id=job.id, call=what, attempt=attempt + 1, wait=wait, error=exc)
            sleep(wait)
            continue
        except LlmProtocolError as exc:
            bad_answers += 1
            shorter = shorter or isinstance(exc, LlmTruncatedError)
            if bad_answers >= 2 or attempt == MAX_ATTEMPTS - 1:
                raise
            log_event(logger, "llm_bad_answer", job_id=job.id, call=what, error=exc)
            continue
        log_event(
            logger,
            "llm_call",
            job_id=job.id,
            call=what,
            model=settings.llm_model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            seconds=round(time.monotonic() - started, 1),
        )
        return summary
    raise AssertionError("unreachable")


def _complete(db: Session, job: AudioJob, summary: Summary) -> NextStep:
    saved = jobs.transition(
        db,
        job.id,
        JobStatus.SUMMARIZING,
        JobStatus.COMPLETED,
        summary=summary.model_dump(),
        summary_partials=None,  # the parts have done their job
    )
    if saved:
        log_event(logger, "summary_saved", job_id=job.id, key_points=len(summary.key_points))
    return None


def _fail(db: Session, job: AudioJob, failure: Failure, cause: Exception | None) -> NextStep:
    """Fail the job. The transcript stays saved (and readable). Always returns None: nothing more is due."""
    if jobs.fail_job(db, job.id, failure):
        log_event(
            logger, "job_failed", job_id=job.id, error_code=failure.code, cause=type(cause).__name__ if cause else None
        )
    return None


def failure_from_error(exc: LlmError, settings: Settings) -> Failure:
    """What the user sees. Provider text goes only to the log, never into the message."""
    if isinstance(exc, LlmTransientError):
        if exc.status_code == 429:
            if exc.retry_after is not None and exc.retry_after > settings.llm_max_wait_seconds:
                minutes = max(1, ceil(exc.retry_after / 60))
                return Failure(
                    failures.SUMMARY_RATE_LIMITED,
                    f"The summary service is rate limited right now. Please try again in about {minutes} "
                    f"minute{'s' if minutes != 1 else ''}. The transcript is ready.",
                )
            return Failure(
                failures.SUMMARY_RATE_LIMITED,
                "The summary service is busy. Please try again in a few minutes. The transcript is ready.",
            )
        return Failure(
            failures.SUMMARY_UNAVAILABLE,
            "The summary service is unavailable right now. Please try again in a few minutes. The transcript is ready.",
        )
    if isinstance(exc, LlmProtocolError):
        return Failure(
            failures.SUMMARY_INVALID_RESPONSE,
            "The summary could not be produced in the expected format. Please try again. The transcript is ready.",
        )
    if isinstance(exc, LlmPermanentError) and exc.status_code in (401, 403, 404):
        return Failure(
            failures.SUMMARY_AUTH_FAILED,
            "Summaries are unavailable because of a configuration problem on our side. The transcript is ready.",
        )
    return Failure(
        failures.SUMMARY_REQUEST_REJECTED,
        "The summary service rejected this request, so no summary could be made. The transcript is ready.",
    )
