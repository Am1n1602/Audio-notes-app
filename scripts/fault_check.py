"""Live failure scenarios (Phase 7): make the real providers (or the real files) misbehave and check, end to end, what the
person is told and that nothing retries without bound.

Needs everything pipeline_check.py needs, plus two fault proxies (scripts/fault_proxy.py) that the WORKER talks through:

    python scripts/fault_proxy.py --upstream https://api.vachana.ai --port 9101 --rewrite-downloads
    python scripts/fault_proxy.py --upstream https://api.groq.com --port 9102
    GNANI_BASE_URL=http://127.0.0.1:9101 LLM_BASE_URL=http://127.0.0.1:9102/openai/v1 GNANI_MAX_TRANSCRIBE_SECONDS=100 \\
        celery -A worker.celery_app worker -l info -P solo        # (PowerShell: set $env:... first)

    python scripts/fault_check.py --speech some-speech.wav                   # every scenario
    python scripts/fault_check.py --speech some-speech.wav --only G2,L2,I1   # some of them

Each scenario installs fault rules, uploads a file, waits for the job to settle, then checks the outcome (status, error
code, whether Retry is offered, how long it took) and how many calls reached the provider (a bound on retrying). Some then
clear the faults, press Retry and check the job recovers without redoing finished work. `--speech` is a short recording of
speech (about 25 s): the checks that need a transcript use it. Exit status 1 if any scenario fails.
"""

from __future__ import annotations

import argparse
import random
import re
import shutil
import sys
import tempfile
import time
import uuid
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pipeline_check import SETTLED, upload

Rules = list[dict[str, Any]]
Entry = dict[str, Any]

# What each counter counts in a proxy's log. A bound on these is the proof that nothing retries forever.
COUNTERS: dict[str, tuple[str, Callable[[Entry], bool]]] = {
    "create": ("gnani", lambda e: e["method"] == "POST" and e["path"] == "/stt/v3/batch/jobs"),
    "start": ("gnani", lambda e: e["method"] == "POST" and e["path"].endswith("/start")),
    "status": (
        "gnani",
        lambda e: e["method"] == "GET" and re.fullmatch(r"/stt/v3/batch/jobs/[^/]+", e["path"]) is not None,
    ),
    "files": ("gnani", lambda e: e["method"] == "GET" and e["path"].endswith("/files")),
    "download": ("gnani", lambda e: e["path"].startswith("/__dl")),
    "llm": ("llm", lambda e: e["method"] == "POST" and e["path"].endswith("/chat/completions")),
}

CREATE = {"match": "/stt/v3/batch/jobs", "exact": True, "method": "POST"}
STATUS = {"match": "/stt/v3/batch/jobs/", "method": "GET"}  # also matches /files, which only runs after a good status
CHAT = {"match": "/chat/completions", "method": "POST"}


@dataclass
class Recovery:
    """After the failure: clear the faults, press Retry, and expect the job to complete."""

    calls: dict[str, tuple[int, int]] = field(default_factory=dict)  # calls to the providers during the retry only


@dataclass
class Scenario:
    id: str
    title: str
    audio: str  # speech | silence | not-audio | corrupt | truncated
    target: str | None = None  # which proxy the rules go to: gnani | llm
    rules: Rules = field(default_factory=list)
    status: str | None = "COMPLETED"  # expected final status; None = report whatever happens
    code: str | None = None  # expected error_code when FAILED
    retryable: bool | None = None  # expected can_retry when FAILED
    seconds: tuple[float, float] = (0, 120)  # how long settling may take (a floor proves a wait really happened)
    calls: dict[str, tuple[int, int]] = field(default_factory=dict)  # (min, max) calls to the providers
    says: str | None = None  # text the person-facing message must contain
    seen: str | None = None  # text the page must show at some point while the job runs
    recovery: Recovery | None = None


def scenarios(deadline: int) -> list[Scenario]:
    forever = -1
    slow = 35  # longer than the client's 30 s read timeout
    return [
        # --- Gnani -----------------------------------------------------------------------------------------------
        Scenario(
            "G1",
            "Create refused with 429 twice, then accepted",
            "speech",
            "gnani",
            [
                {
                    **CREATE,
                    "action": "status",
                    "status": 429,
                    "times": 2,
                    "body": {"error": "RATE_LIMITED", "message": "slow down"},
                }
            ],
            seconds=(6, 120),
            calls={"create": (3, 3)},
        ),
        Scenario(
            "G2",
            "Create succeeds at Gnani but the reply is lost (503)",
            "speech",
            "gnani",
            [{**CREATE, "action": "forward_then_fail", "status": 503}],
            status="FAILED",
            code="SUBMIT_UNCONFIRMED",
            retryable=True,
            seconds=(0, 60),
            calls={"create": (1, 1)},
            says="could not confirm",
            recovery=Recovery({"create": (1, 1)}),
        ),
        Scenario(
            "G3",
            "Create hangs past the 30 s read timeout",
            "speech",
            "gnani",
            [{**CREATE, "action": "hang", "seconds": slow}],
            status="FAILED",
            code="SUBMIT_UNCONFIRMED",
            retryable=True,
            seconds=(28, 90),
            calls={"create": (1, 1)},
        ),
        Scenario(
            "G4",
            "Start refused with 503 five times, finished by the next poll",
            "speech",
            "gnani",
            [{"match": "/start", "method": "POST", "action": "status", "status": 503, "times": 5}],
            seconds=(30, 150),
            calls={"create": (1, 1), "start": (6, 6)},
        ),
        Scenario(
            "G5",
            "Status polls refused with 503 twice",
            "speech",
            "gnani",
            [{**STATUS, "action": "status", "status": 503, "times": 2}],
            seconds=(55, 200),
            seen="Waiting for the transcription service",
            calls={"create": (1, 1), "status": (3, 5)},
        ),
        Scenario(
            "G6",
            "Status polls rate limited (429) three times",
            "speech",
            "gnani",
            [{**STATUS, "action": "status", "status": 429, "times": 3}],
            seconds=(14, 120),
            calls={"create": (1, 1), "status": (4, 8)},
        ),
        Scenario(
            "G7",
            f"Gnani down for good (status always 503); deadline {deadline} s",
            "speech",
            "gnani",
            [{**STATUS, "action": "status", "status": 503, "times": forever}],
            status="FAILED",
            code="TRANSCRIPTION_UNAVAILABLE",
            retryable=True,
            seconds=(deadline - 5, deadline + 90),
            seen="Waiting for the transcription service",
            calls={"create": (1, 1), "status": (2, deadline // 20 + 3)},
            recovery=Recovery({"create": (1, 1)}),
        ),
        Scenario(
            "G8",
            f"Gnani never finishes (always IN_PROGRESS); deadline {deadline} s",
            "speech",
            "gnani",
            [
                {
                    **STATUS,
                    "exact": False,
                    "action": "synthetic",
                    "times": forever,
                    "body": {
                        "status": "IN_PROGRESS",
                        "progress": {"total_files": 1, "completed_files": 0, "failed_files": 0},
                    },
                }
            ],
            status="FAILED",
            code="TRANSCRIPTION_TIMEOUT",
            retryable=True,
            seconds=(deadline - 5, deadline + 60),
            calls={"create": (1, 1), "status": (deadline // 10 - 2, deadline // 10 + 3)},
        ),
        Scenario(
            "G9",
            "Transcript download refused with 503 twice",
            "speech",
            "gnani",
            [{"match": "/__dl", "action": "status", "status": 503, "times": 2}],
            seconds=(55, 200),
            seen="Waiting for the transcription service",
            calls={"create": (1, 1), "download": (3, 3)},
        ),
        Scenario(
            "G10",
            "Transcript link expired (403)",
            "speech",
            "gnani",
            [{"match": "/__dl", "action": "status", "status": 403, "times": 1}],
            status="FAILED",
            code="TRANSCRIPTION_FAILED",
            retryable=True,
            seconds=(0, 90),
            calls={"create": (1, 1), "download": (1, 1)},
            says="could not be retrieved",
            recovery=Recovery({"create": (1, 1), "download": (1, 1)}),  # a failed download means transcribing AGAIN
        ),
        Scenario(
            "G11",
            "Wrong Gnani API key (401)",
            "speech",
            "gnani",
            [
                {
                    **CREATE,
                    "action": "status",
                    "status": 401,
                    "body": {"detail": {"error_code": "INVALID_API_KEY", "message": "bad key", "status_code": 401}},
                }
            ],
            status="FAILED",
            code="TRANSCRIPTION_AUTH_FAILED",
            retryable=False,
            seconds=(0, 60),
            calls={"create": (1, 1)},
        ),
        Scenario(
            "G12",
            "Gnani answers Create with a web page instead of JSON",
            "speech",
            "gnani",
            [{**CREATE, "action": "garbage"}],
            status="FAILED",
            code="TRANSCRIPTION_FAILED",
            retryable=True,
            seconds=(0, 60),
            calls={"create": (1, 1)},
        ),
        # --- the audio itself (real Gnani, no fault injected) ---------------------------------------------------------
        Scenario(
            "I1",
            "A text file named .wav",
            "not-audio",
            status="FAILED",
            code="INVALID_AUDIO",
            retryable=False,
            seconds=(0, 90),
        ),
        Scenario(
            "I2", "Silence", "silence", status="FAILED", code="EMPTY_TRANSCRIPT", retryable=False, seconds=(0, 90)
        ),
        Scenario(
            "I3",
            "Random bytes named .mp3",
            "corrupt",
            status="FAILED",
            code="INVALID_AUDIO",
            retryable=False,
            seconds=(0, 90),
        ),
        Scenario("I4", "A WAV cut off after 100 KB (header claims more)", "truncated", status=None, seconds=(0, 120)),
        # --- the summary model ----------------------------------------------------------------------------------------
        Scenario(
            "L1",
            "First model call times out, the retry works",
            "speech",
            "llm",
            [{**CHAT, "action": "hang", "seconds": slow, "times": 1}],
            seconds=(30, 120),
            calls={"llm": (2, 2)},
        ),
        Scenario(
            "L2",
            "Model down for good (503); then it recovers and Retry resumes the summary",
            "speech",
            "llm",
            [{**CHAT, "action": "status", "status": 503, "times": forever}],
            status="FAILED",
            code="SUMMARY_UNAVAILABLE",
            retryable=True,
            seconds=(28, 120),
            calls={"llm": (5, 5)},
            says="transcript is ready",
            recovery=Recovery({"create": (0, 0), "llm": (1, 1)}),
        ),
        Scenario(
            "L3",
            "Every model call times out",
            "speech",
            "llm",
            [{**CHAT, "action": "hang", "seconds": slow, "times": forever}],
            status="FAILED",
            code="SUMMARY_UNAVAILABLE",
            retryable=True,
            seconds=(90, 260),
            calls={"llm": (3, 5)},
        ),
        Scenario(
            "L4",
            "Model rate limited with a 3 s retry-after, once",
            "speech",
            "llm",
            [{**CHAT, "action": "status", "status": 429, "headers": {"retry-after": "3"}, "times": 1}],
            seconds=(3, 90),
            calls={"llm": (2, 2)},
        ),
        Scenario(
            "L5",
            "Model rate limited for 120 s",
            "speech",
            "llm",
            [{**CHAT, "action": "status", "status": 429, "headers": {"retry-after": "120"}, "times": forever}],
            status="FAILED",
            code="SUMMARY_RATE_LIMITED",
            retryable=True,
            seconds=(0, 60),
            calls={"llm": (1, 1)},
            says="2 minutes",
        ),
        Scenario(
            "L6",
            "Model answers with something that is not JSON",
            "speech",
            "llm",
            [{**CHAT, "action": "garbage", "times": forever}],
            status="FAILED",
            code="SUMMARY_INVALID_RESPONSE",
            retryable=True,
            seconds=(0, 60),
            calls={"llm": (2, 2)},
        ),
        Scenario(
            "L7",
            "Wrong model API key (401)",
            "speech",
            "llm",
            [
                {
                    **CHAT,
                    "action": "status",
                    "status": 401,
                    "times": forever,
                    "body": {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}},
                }
            ],
            status="FAILED",
            code="SUMMARY_AUTH_FAILED",
            retryable=False,
            seconds=(0, 60),
            calls={"llm": (1, 1)},
        ),
    ]


class Lab:
    """The API, the two proxies and the audio files a scenario needs."""

    def __init__(self, api: str, proxies: dict[str, str], speech: Path, client_id: str) -> None:
        self.api = httpx.Client(base_url=api, timeout=60, headers={"X-Client-Id": client_id})
        self.proxies = proxies
        self.dir = Path(tempfile.mkdtemp(prefix="fault-check-"))
        self.speech = speech

    def file_for(self, scenario: Scenario) -> Path:
        name = re.sub(r"[^a-z0-9]+", "-", f"{scenario.id}-{scenario.title}".lower()).strip("-")[:60]
        kind, ext = {"corrupt": ("corrupt", ".mp3")}.get(scenario.audio, (scenario.audio, ".wav"))
        path = self.dir / f"{name}{ext}"
        if kind == "speech":
            shutil.copy(self.speech, path)
        elif kind == "truncated":
            path.write_bytes(self.speech.read_bytes()[:100_000])
        elif kind == "corrupt":
            path.write_bytes(random.Random(1).randbytes(200_000))
        elif kind == "not-audio":
            path.write_bytes(b"This is just text, not audio.\n" * 500)
        elif kind == "silence":
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(bytes(32000 * 6))
        return path

    def reset(self) -> None:
        for url in self.proxies.values():
            httpx.delete(f"{url}/__rules")
            httpx.delete(f"{url}/__log")

    def set_rules(self, target: str | None, rules: Rules) -> None:
        if target:
            httpx.put(f"{self.proxies[target]}/__rules", json=rules).raise_for_status()

    def log(self) -> dict[str, list[Entry]]:
        return {name: httpx.get(f"{url}/__log").json() for name, url in self.proxies.items()}

    def settle(self, job_id: str, limit: float) -> tuple[dict[str, Any], float, list[str]]:
        started = time.monotonic()
        timeline: list[str] = []
        last = None
        while True:
            job = self.api.get(f"/api/uploads/{job_id}").json()
            key = (job["status"], job["progress_message"] or job["error_code"])
            if key != last:
                last = key
                timeline.append(
                    f"{time.monotonic() - started:.0f}s {job['status']}" + (f" ({key[1]})" if key[1] else "")
                )
            elapsed = time.monotonic() - started
            if job["status"] in SETTLED or elapsed > limit:
                return job, elapsed, timeline
            time.sleep(2)


def counts(log: dict[str, list[Entry]], since: dict[str, int]) -> dict[str, int]:
    result = {}
    for name, (proxy, matches) in COUNTERS.items():
        result[name] = sum(1 for e in log[proxy][since.get(proxy, 0) :] if matches(e))
    return result


def check_calls(expected: dict[str, tuple[int, int]], seen: dict[str, int], problems: list[str], when: str) -> None:
    for name, (low, high) in expected.items():
        if not low <= seen[name] <= high:
            problems.append(f"{when}: {name} calls {seen[name]}, expected {low} to {high}")


def run(scenario: Scenario, lab: Lab) -> tuple[bool, list[str]]:
    lab.reset()
    lab.set_rules(scenario.target, scenario.rules)
    path = lab.file_for(scenario)
    job_id, _, _ = upload(lab.api, path)
    job, elapsed, timeline = lab.settle(job_id, scenario.seconds[1] + 90)
    log = lab.log()
    seen = counts(log, {})
    problems: list[str] = []
    if scenario.status and job["status"] != scenario.status:
        problems.append(f"status {job['status']}, expected {scenario.status}")
    if job["status"] == "FAILED":
        if scenario.code and job["error_code"] != scenario.code:
            problems.append(f"error code {job['error_code']}, expected {scenario.code}")
        if scenario.retryable is not None and job["can_retry"] is not scenario.retryable:
            problems.append(f"can_retry {job['can_retry']}, expected {scenario.retryable}")
        if scenario.says and scenario.says.lower() not in (job["error_message"] or "").lower():
            problems.append(f"message lacks {scenario.says!r}")
    if not scenario.seconds[0] <= elapsed <= scenario.seconds[1]:
        problems.append(f"settled in {elapsed:.0f}s, expected {scenario.seconds[0]:.0f} to {scenario.seconds[1]:.0f}")
    if job["status"] not in SETTLED:
        problems.append("never settled")
    if scenario.seen and not any(scenario.seen in step for step in timeline):
        problems.append(f"the page never said {scenario.seen!r} while it waited")
    check_calls(scenario.calls, seen, problems, "first run")

    detail = f"{job['status']}" + (f"/{job['error_code']}" if job["error_code"] else "")
    print(f"{'PASS' if not problems else 'FAIL'}  {scenario.id:<4}{scenario.title}")
    print(
        f"      {detail}  retry={'offered' if job['can_retry'] else 'not offered'}  {elapsed:.0f}s  calls "
        + " ".join(f"{k}={v}" for k, v in seen.items() if v)
    )
    print(f"      {' | '.join(timeline)}")
    if job["error_message"]:
        print(f"      says: {job['error_message']}")

    if scenario.recovery and job["status"] == "FAILED":
        lab.reset()  # the faults are over; counting starts again
        retried = lab.api.post(f"/api/uploads/{job_id}/retry")
        if retried.status_code != 200:
            problems.append(f"retry refused: {retried.status_code}")
        else:
            again, took, steps = lab.settle(job_id, 150)
            after = counts(lab.log(), {})
            if again["status"] != "COMPLETED":
                problems.append(f"after retry: {again['status']} ({again['error_code']})")
            if again["status"] == "COMPLETED" and not again["summary"]:
                problems.append("after retry: completed without a summary")
            check_calls(scenario.recovery.calls, after, problems, "retry")
            print(
                f"      retry -> {again['status']} in {took:.0f}s, calls "
                + " ".join(f"{k}={v}" for k, v in after.items() if v)
            )
    for problem in problems:
        print(f"      !! {problem}")
    return not problems, problems


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]  # progress shows up as it happens
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--gnani-proxy", default="http://127.0.0.1:9101")
    parser.add_argument("--llm-proxy", default="http://127.0.0.1:9102")
    parser.add_argument("--speech", type=Path, required=True, help="a short recording of speech (WAV)")
    parser.add_argument("--only", help="comma-separated scenario ids, e.g. G2,L2")
    parser.add_argument("--deadline", type=int, default=100, help="the worker's GNANI_MAX_TRANSCRIBE_SECONDS")
    parser.add_argument(
        "--client-id", default=str(uuid.uuid4()), help="act as this browser, to see the jobs in its history"
    )
    args = parser.parse_args()

    lab = Lab(args.api, {"gnani": args.gnani_proxy, "llm": args.llm_proxy}, args.speech, args.client_id)
    chosen = {s.strip().upper() for s in args.only.split(",")} if args.only else None
    results: list[tuple[str, bool]] = []
    try:
        for scenario in scenarios(args.deadline):
            if chosen is None or scenario.id in chosen:
                results.append((scenario.id, run(scenario, lab)[0]))
    finally:
        lab.reset()
        shutil.rmtree(lab.dir, ignore_errors=True)
    failed = [name for name, ok in results if not ok]
    print(
        f"\n{len(results) - len(failed)} of {len(results)} scenarios passed"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
