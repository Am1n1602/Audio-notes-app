"""Live end-to-end check of the whole pipeline: upload -> queue -> worker -> Gnani -> transcript -> summary.

Needs the API, the Celery worker, Postgres, Redis, real S3, a real Gnani key and a real Groq key all running.

    python scripts/pipeline_check.py http://127.0.0.1:8000 recording.wav
    python scripts/pipeline_check.py http://127.0.0.1:8000 silence.wav --expect-error EMPTY_TRANSCRIPT
    python scripts/pipeline_check.py http://127.0.0.1:8000 two-hours.mp3 --timeout 180

Prints a timeline of every status / stage message the job goes through, then checks the outcome.
Without --expect-error the job must reach COMPLETED with a transcript and a summary.

--timeout MINUTES   how long to wait for the job to settle (default 5; a long recording needs far more)
--client-id UUID    act as that browser, so the job also shows up in that browser's history (default: a fresh id)
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

POLL_EVERY_SECONDS = 3
SETTLED = {"COMPLETED", "FAILED"}


def blocks(path: Path, size: int = 1024 * 1024) -> Iterator[bytes]:
    """The file in 1 MB pieces, so a multi-gigabyte recording is never held in memory."""
    with path.open("rb") as handle:
        while block := handle.read(size):
            yield block


def upload(api: httpx.Client, audio: Path, *, quiet: bool = True) -> tuple[str, dict[str, Any], float]:
    """initiate -> PUT to storage -> /complete, like the page does. Returns the job id, /complete's answer and the
    seconds the transfer took."""
    size = audio.stat().st_size
    init = api.post("/api/uploads/initiate", json={"filename": audio.name, "size_bytes": size})
    assert init.status_code == 201, init.text
    job_id, target = init.json()["id"], init.json()["upload"]
    sent = time.monotonic()
    # The size is signed into the link, so it must be sent as a Content-Length (not chunked) even though it streams.
    headers = {**target["headers"], "Content-Length": str(size)}
    put = httpx.put(target["url"], content=blocks(audio), headers=headers, timeout=httpx.Timeout(60, read=300))
    assert put.status_code == 200, f"upload to S3 failed: HTTP {put.status_code}"
    seconds = time.monotonic() - sent
    # Like the real UI: /complete answers 409 UPLOAD_NOT_FOUND or 503 STORAGE_UNAVAILABLE when it is safe to try again.
    for attempt in range(1, 4):
        done = api.post(f"/api/uploads/{job_id}/complete")
        code = done.json().get("error", {}).get("code") if done.status_code != 200 else None
        if done.status_code == 200 or code not in ("UPLOAD_NOT_FOUND", "STORAGE_UNAVAILABLE"):
            break
        if not quiet:
            print(f"  /complete answered {code} (attempt {attempt}); retrying")
        time.sleep(2)
    assert done.status_code == 200, done.text
    return str(job_id), done.json(), seconds


def main(base_url: str, audio: Path, expect_error: str | None, timeout_minutes: float, client_id: str) -> int:
    # Each browser has its own random client id; a job is only visible to the id that created it.
    api = httpx.Client(base_url=base_url, timeout=60, headers={"X-Client-Id": client_id})
    job_id, done, seconds = upload(api, audio, quiet=False)
    print(f"job {job_id}  ({audio.stat().st_size:,} bytes, uploaded in {seconds:.0f}s)")

    started = time.monotonic()
    seen: list[tuple[str, str | None]] = []
    job: dict[str, Any] = done
    while True:
        key = (job["status"], job["progress_message"] or job["error_code"])
        if not seen or key != seen[-1]:
            seen.append(key)
            print(
                f"  {time.monotonic() - started:6.1f}s  {job['status']:<13} {job['progress_message'] or job['error_code'] or ''}"
            )
        if job["status"] in SETTLED:
            break
        if time.monotonic() - started > timeout_minutes * 60:
            print(f"FAIL: still {job['status']} after {timeout_minutes:g} minutes")
            return 1
        time.sleep(POLL_EVERY_SECONDS)
        job = api.get(f"/api/uploads/{job_id}").json()

    problems: list[str] = []
    if expect_error:
        if job["status"] != "FAILED" or job["error_code"] != expect_error:
            problems.append(f"expected FAILED/{expect_error}, got {job['status']}/{job['error_code']}")
        print(f"  message: {job['error_message']}   can_retry={job['can_retry']}")
    else:
        if job["status"] != "COMPLETED":
            problems.append(f"expected COMPLETED, got {job['status']} ({job['error_code']}: {job['error_message']})")
        if not (job["transcript"] or "").strip():
            problems.append("no transcript saved")
        else:
            text = job["transcript"]
            print(f"  transcript ({len(text)} chars, {job['duration_seconds']} s of audio): {text[:110]}...")
        summary = job["summary"]
        if not summary:
            problems.append("no summary saved")
        else:
            print(f"  overview: {summary['overview']}")
            for name in ("key_points", "action_items", "decisions", "uncertainties"):
                print(f"  {name}:")
                for line in summary[name]:
                    print(f"    - {line}")
    print("FAILED: " + "; ".join(problems) if problems else "pipeline check passed")
    return 1 if problems else 0


if __name__ == "__main__":
    # Summaries come back in the person's language (Hindi, a rupee sign ...). A Windows console, or output sent to a pipe
    # or file, defaults to a legacy code page that cannot print them and would crash AFTER a long run had finished.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--expect-error", metavar="CODE")
    parser.add_argument("--timeout", type=float, default=5, metavar="MINUTES")
    parser.add_argument("--client-id", default=None, metavar="UUID")
    args = parser.parse_args()
    sys.exit(
        main(
            args.base_url.rstrip("/"), args.audio, args.expect_error, args.timeout, args.client_id or str(uuid.uuid4())
        )
    )
