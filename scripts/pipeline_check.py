"""Live end-to-end check of the whole pipeline: upload -> queue -> worker -> Gnani -> transcript -> summary.

Needs the API, the Celery worker, Postgres, Redis, real S3, a real Gnani key and a real Groq key all running.

    python scripts/pipeline_check.py http://127.0.0.1:8000 recording.wav
    python scripts/pipeline_check.py http://127.0.0.1:8000 silence.wav --expect-error EMPTY_TRANSCRIPT

Prints a timeline of every status / stage message the job goes through, then checks the outcome.
Without --expect-error the job must reach COMPLETED with a transcript and a summary.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

POLL_EVERY_SECONDS = 3
GIVE_UP_AFTER_SECONDS = 300
SETTLED = {"COMPLETED", "FAILED"}


def main(base_url: str, audio: Path, expect_error: str | None) -> int:
    data = audio.read_bytes()
    # Each browser has its own random client id; a job is only visible to the id that created it.
    api = httpx.Client(base_url=base_url, timeout=60, headers={"X-Client-Id": str(uuid.uuid4())})

    init = api.post("/api/uploads/initiate", json={"filename": audio.name, "size_bytes": len(data)})
    assert init.status_code == 201, init.text
    job_id, target = init.json()["id"], init.json()["upload"]
    put = httpx.put(target["url"], content=data, headers=target["headers"], timeout=300)
    assert put.status_code == 200, f"upload to S3 failed: HTTP {put.status_code}"
    # Like the real UI: /complete answers 409 UPLOAD_NOT_FOUND or 503 STORAGE_UNAVAILABLE when it is safe to try again.
    for attempt in range(1, 4):
        done = api.post(f"/api/uploads/{job_id}/complete")
        code = done.json().get("error", {}).get("code") if done.status_code != 200 else None
        if done.status_code == 200 or code not in ("UPLOAD_NOT_FOUND", "STORAGE_UNAVAILABLE"):
            break
        print(f"  /complete answered {code} (attempt {attempt}); retrying")
        time.sleep(2)
    assert done.status_code == 200, done.text
    print(f"job {job_id}  ({len(data):,} bytes)")

    started = time.monotonic()
    seen: list[tuple[str, str | None]] = []
    job: dict[str, Any] = done.json()
    while True:
        key = (job["status"], job["progress_message"] or job["error_code"])
        if not seen or key != seen[-1]:
            seen.append(key)
            print(
                f"  {time.monotonic() - started:6.1f}s  {job['status']:<13} {job['progress_message'] or job['error_code'] or ''}"
            )
        if job["status"] in SETTLED:
            break
        if time.monotonic() - started > GIVE_UP_AFTER_SECONDS:
            print(f"FAIL: still {job['status']} after {GIVE_UP_AFTER_SECONDS}s")
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    expect = sys.argv[sys.argv.index("--expect-error") + 1] if "--expect-error" in sys.argv else None
    if expect:
        args.remove(expect)
    if len(args) != 2:
        sys.exit(__doc__)
    sys.exit(main(args[0].rstrip("/"), Path(args[1]), expect))
