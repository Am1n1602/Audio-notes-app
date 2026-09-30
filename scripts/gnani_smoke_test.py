"""Phase 0 smoke test: prove the real Gnani Batch STT flow from the command line."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("GNANI_BASE_URL", "https://api.vachana.ai").rstrip("/")
JOBS = "/stt/v3/batch/jobs"
TERMINAL = {"COMPLETED", "PARTIAL_FAILURE", "FAILED", "START_FAILED", "CANCELLED"}
RETRY_STATUS = {429, 500, 502, 503, 504}
AUDIO_EXTS = {".wav", ".mp3", ".mp4", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".webm", ".amr"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # direct multipart cap; URL sources have no byte cap
MIN_POLL_SECONDS = 10  # docs: poll no faster than every 10s

Api = Callable[..., httpx.Response]


class GnaniError(Exception):
    """A failure worth showing to the user as-is. `status` is the HTTP status, if there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AmbiguousCreateError(GnaniError):
    """Create failed in a way that may still have produced a job at Gnani. Never retried automatically."""


# Failures where the request provably never reached Gnani's application, so retrying cannot duplicate work.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def describe(resp: httpx.Response) -> str:
    """Gnani documents two error shapes: {error, message} and {detail: {error_code, message}}."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body["detail"] if isinstance(body.get("detail"), dict) else body
        text = " - ".join(str(p) for p in (err.get("error_code") or err.get("error"), err.get("message")) if p)
        if text:
            return text
    return resp.text[:300] or resp.reason_phrase


def send(
    client: httpx.Client,
    method: str,
    url: str,
    key: str | None = None,
    retries: int = 4,
    idempotent: bool = True,
    **kw: Any,
) -> httpx.Response:
    """One HTTP call. Retries transient failures (network, 429, 5xx) with exponential backoff + jitter;
    anything else 4xx fails immediately. Pass key=None for non-Gnani hosts (transcript_url is a
    presigned S3 URL and must not receive the API key).

    idempotent=False (Create Job): only retry when the request certainly never reached Gnani (connect
    failure, or a 429 refusal). A timeout or 5xx may mean the job was created, so raise
    AmbiguousCreateError instead of risking a duplicate job.

    Error messages never include external URLs or exception text: both can embed a signed URL.
    """
    headers = {"X-API-Key-ID": key} if key else {}
    where = f"{method} {url}" if url.startswith("/") else f"{method} <external url>"
    for attempt in range(retries + 1):
        status = None
        try:
            resp = client.request(method, url, headers=headers, **kw)
        except httpx.TransportError as exc:
            failure, not_processed = type(exc).__name__, isinstance(exc, NOT_SENT)
        else:
            if resp.status_code < 400:
                return resp
            status, failure = resp.status_code, f"HTTP {resp.status_code}: {describe(resp)}"
            if status not in RETRY_STATUS:
                hint = " (check GNANI_API_KEY)" if status in (401, 403) else ""
                raise GnaniError(f"{where} -> {failure}{hint}", status)
            not_processed = status == 429
        if not idempotent and not not_processed:
            raise AmbiguousCreateError(
                f"{where}: {failure}. Cannot tell whether Gnani created a job (Create has no idempotency key), "
                "so it is not retried automatically; an orphan job may exist at Gnani",
                status,
            )
        if attempt == retries:
            raise GnaniError(f"{where} failed after {retries + 1} attempts: {failure}", status)
        delay = 2 ** (attempt + 1) + random.random()  # ~2s, 4s, 8s, 16s
        log(f"  {where}: {failure}; retry {attempt + 1}/{retries} in {delay:.0f}s")
        time.sleep(delay)
    raise AssertionError("unreachable")


def json_body(resp: httpx.Response, *keys: str) -> dict[str, Any]:
    """Parse a 2xx body and check the keys we depend on, so a schema change fails loudly."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or any(k not in data for k in keys):
        raise GnaniError(f"unexpected response shape from Gnani (expected keys {list(keys)})")
    return data


def create_job(api: Api, source: str, config: dict[str, Any]) -> str:
    # Create has no documented idempotency key, so it is sent with idempotent=False (see send()).
    # The real worker must also persist gnani_job_id right after this call, before doing anything else.
    if source.lower().startswith(("http://", "https://")):
        if not source.lower().startswith("https://"):
            raise GnaniError("source URL must be https://")
        if source.split("?")[0].endswith("/"):
            raise GnaniError("URL ends with '/': Gnani expands that as a folder; pass one file URL")
        body = {
            "config": config,
            "source": {"type": "cloud_storage", "auth": {"mode": "public"}, "paths": [source]},
        }
        resp = api("POST", JOBS, idempotent=False, json=body)
    else:
        path = Path(source)
        if not path.is_file():
            raise GnaniError(f"file not found: {source}")
        if path.suffix.lower() not in AUDIO_EXTS:
            raise GnaniError(f"unsupported extension {path.suffix!r}; Gnani Batch accepts {sorted(AUDIO_EXTS)}")
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise GnaniError(
                f"{size / 1e6:.1f} MB exceeds the 10 MB direct-upload cap; "
                "put it in object storage and pass a presigned https:// URL instead"
            )
        files = {
            "config": (None, json.dumps(config), "application/json"),
            "files": (
                path.name,
                path.read_bytes(),
                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            ),
        }
        resp = api("POST", JOBS, idempotent=False, files=files)
    return json_body(resp, "job_id")["job_id"]


def run(
    client: httpx.Client,
    key: str,
    source: str,
    language: str = "en-IN",
    poll_seconds: float = MIN_POLL_SECONDS,
    timeout_seconds: float = 1800,
) -> dict[str, Any]:
    """Create -> start -> poll -> files -> transcript. Raises GnaniError if the job ends FAILED /
    START_FAILED / CANCELLED; per-file failures in a finished job (e.g. silent audio) come back in
    result["errors"]."""

    def api(method: str, path: str, **kw: Any) -> httpx.Response:
        return send(client, method, path, key, **kw)

    config = {
        "model": "gnani-prisma-v2.5",
        "language_code": language,
        "mode": "transcribe",
        "with_diarization": False,
        "is_multi_channel": False,
        "with_denoise": False,
    }
    job_id = create_job(api, source, config)
    log(f"created job_id={job_id}")

    try:
        api("POST", f"{JOBS}/{job_id}/start")
    except GnaniError as exc:
        if exc.status != 409:  # 409 = already started (e.g. our first attempt landed but the reply was lost)
            raise
        log("start returned 409 (already started?); job status below is authoritative")

    t0 = time.monotonic()
    while True:
        job = json_body(api("GET", f"{JOBS}/{job_id}"), "status")
        status = job["status"]
        p = job.get("progress") or {}
        log(
            f"[{time.monotonic() - t0:4.0f}s] {status} ({p.get('completed_files', '?')}/{p.get('total_files', '?')} files)"
        )
        if status in TERMINAL:
            break
        if time.monotonic() - t0 >= timeout_seconds:
            raise GnaniError(f"still {status} after {timeout_seconds:.0f}s; job {job_id} keeps running at Gnani")
        time.sleep(poll_seconds)

    result: dict[str, Any] = {"job_id": job_id, "transcripts": [], "errors": []}
    for f in json_body(api("GET", f"{JOBS}/{job_id}/files"), "data")["data"]:
        file_id = f.get("file_id")
        if f.get("status") != "COMPLETED" or not f.get("transcript_url"):
            result["errors"].append(f"file {file_id} {f.get('status')}: {f.get('error_message')}")
            continue
        # transcript_url expires after 1 hour, so fetch it right away (and without the API key).
        transcript = json_body(send(client, "GET", f["transcript_url"]), "full_transcript")
        if not (transcript["full_transcript"] or "").strip():
            result["errors"].append(f"file {file_id}: empty transcript")
            continue
        result["transcripts"].append({"file_id": file_id, "transcript": transcript})

    if status not in ("COMPLETED", "PARTIAL_FAILURE"):
        # A failed job carries no reason at job level; the cause (e.g. "public download: HTTP 406") is per file.
        reason = job.get("cancel_reason") or "; ".join(result["errors"]) or "no reason given"
        raise GnaniError(f"job {job_id} ended {status}: {reason}")
    return result


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252; transcripts are Indic scripts

    ap = argparse.ArgumentParser(description="Gnani Batch STT smoke test")
    ap.add_argument("source", help="local audio file (<= 10 MB) or https:// URL (presigned URLs work)")
    ap.add_argument("--language", default="en-IN", help="e.g. en-IN, hi-IN, or up to 3 comma-separated codes")
    ap.add_argument("--poll", type=float, default=MIN_POLL_SECONDS, help="seconds between status polls (>= 10)")
    ap.add_argument("--timeout", type=float, default=1800, help="give up polling after this many seconds")
    ap.add_argument("--raw", action="store_true", help="print the full transcript JSON instead of full_transcript")
    args = ap.parse_args()
    if args.poll < MIN_POLL_SECONDS:
        ap.error(f"--poll must be >= {MIN_POLL_SECONDS} (Gnani docs)")
    key = os.environ.get("GNANI_API_KEY") or sys.exit("GNANI_API_KEY is not set (see .env.example)")

    try:
        with httpx.Client(base_url=BASE_URL, timeout=60) as client:
            result = run(client, key, args.source, args.language, args.poll, args.timeout)
    except GnaniError as exc:
        sys.exit(f"ERROR: {exc}")

    for t in result["transcripts"]:
        data = t["transcript"]
        log(
            f"job_id={result['job_id']} file_id={t['file_id']} language={data.get('language_code')} "
            f"duration={data.get('duration_seconds')}s segments={len(data.get('segments') or [])}"
        )
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.raw else data["full_transcript"])
    for err in result["errors"]:
        log(f"ERROR (job_id={result['job_id']}): {err}")
    sys.exit(1 if result["errors"] or not result["transcripts"] else 0)


if __name__ == "__main__":
    main()
