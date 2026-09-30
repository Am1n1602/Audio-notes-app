"""Gnani Batch STT client: the only module that knows Gnani's HTTP API.

Design rule: this client raises typed errors and NEVER retries. The retry policy lives in one place, the
orchestration in services/transcription.py, where it can be tested without waiting. The one thing it does wait for
is pacing: Gnani allows about one API call per second per key, so calls are spaced out (see GnaniClient._pace).

Error types (what the caller may safely do next):
  GnaniTransientError     worth retrying: network failure, 429, 5xx. `not_processed` says whether the request
                          provably never reached Gnani's application (then even Create may be repeated).
  GnaniPermanentError     retrying cannot help (bad request, bad credentials, unknown job, ...).
  GnaniAmbiguousCreateError  Create Job failed in a way that may still have produced a job. Never repeat it.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import Settings, get_settings, require_secret

BATCH_MODEL = "gnani-prisma-v2.5"
JOBS_PATH = "/stt/v3/batch/jobs"
SUCCESS_STATUSES = frozenset({"COMPLETED", "PARTIAL_FAILURE"})
TERMINAL_STATUSES = SUCCESS_STATUSES | {"FAILED", "START_FAILED", "CANCELLED"}

# Failures where the request provably never reached Gnani's application, so repeating it cannot duplicate work.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_SERVER_ERRORS = frozenset({500, 502, 503, 504})


class GnaniError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code  # Gnani's own error code (e.g. RATE_LIMITED, UNSUPPORTED_LANGUAGE), if it sent one


class GnaniTransientError(GnaniError):
    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None, not_processed: bool
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)
        self.not_processed = not_processed


class GnaniPermanentError(GnaniError):
    pass


class GnaniProtocolError(GnaniPermanentError):
    """A 2xx answer that does not have the shape the docs promise (the API changed). Retrying will not fix it."""


class GnaniAmbiguousCreateError(GnaniError):
    pass


@dataclass(frozen=True)
class BatchJob:
    status: str
    cancel_reason: str | None
    total_files: int
    completed_files: int
    failed_files: int


@dataclass(frozen=True)
class BatchFile:
    file_id: str | None
    status: str  # COMPLETED | FAILED | SKIPPED | CANCELLED
    transcript_url: str | None  # a presigned link that expires after an hour: use it at once, never store it
    error_message: str | None


@dataclass(frozen=True)
class Transcript:
    text: str
    duration_seconds: float | None
    language_code: str | None


class GnaniApi(Protocol):
    """What the worker needs from Gnani. GnaniClient is the real one; tests use an in-memory fake."""

    def create_batch_job(self, source_url: str, language_code: str) -> str: ...

    def start_batch_job(self, job_id: str) -> None: ...

    def get_batch_job(self, job_id: str) -> BatchJob: ...

    def get_batch_files(self, job_id: str) -> list[BatchFile]: ...

    def download_transcript(self, url: str) -> Transcript: ...


def _describe(resp: httpx.Response) -> tuple[str | None, str]:
    """Gnani documents two error shapes: {error, message} and {detail: {error_code, message}}."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body["detail"] if isinstance(body.get("detail"), dict) else body
        code = err.get("error_code") or err.get("error")
        message = err.get("message")
        if code or message:
            return (str(code) if code else None), str(message or code)
    return None, (resp.text[:200] or resp.reason_phrase)


def _json(resp: httpx.Response, *keys: str) -> dict[str, Any]:
    """Parse a 2xx body and check the keys we depend on, so a schema change fails loudly."""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or any(key not in data for key in keys):
        raise GnaniProtocolError(f"unexpected response shape from Gnani (expected keys {list(keys)})")
    return data


def _count(value: object) -> int:
    return value if isinstance(value, int) else 0


class GnaniClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        http: httpx.Client | None = None,
        *,
        min_call_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5)
        )
        self._min_interval = min_call_interval_seconds
        self._clock, self._sleep = clock, sleep
        self._last_call: float | None = None
        self._pace_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GnaniClient":
        return cls(
            require_secret(settings.gnani_api_key, "GNANI_API_KEY"),
            settings.gnani_base_url,
            min_call_interval_seconds=settings.gnani_min_call_interval_seconds,
        )

    def _pace(self) -> None:
        """Space Gnani API calls at least `min_call_interval_seconds` apart. Measured live: calls 1 s apart all
        succeeded, calls 0.5 s apart were refused (429) half the time, back-to-back calls mostly. The old habit of
        firing "get job" and "get files" 0.1 s apart therefore hit the limit almost every time."""
        if self._min_interval <= 0:
            return
        with self._pace_lock:
            if self._last_call is not None:
                wait = self._min_interval - (self._clock() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            self._last_call = self._clock()

    def _call(self, method: str, url: str, *, authenticated: bool = True, **kwargs: Any) -> httpx.Response:
        """One HTTP call. Error text never contains an external URL or exception text: both can embed a signed URL."""
        internal = url.startswith("/")
        if internal:
            self._pace()  # only Gnani's API is rate limited; the transcript link is S3
        where = f"{method} {url}" if internal else f"{method} <external url>"
        # The API key goes ONLY to Gnani. transcript_url is a presigned S3 link: sending the key there would leak it.
        headers = {"X-API-Key-ID": self._api_key} if authenticated else {}
        try:
            resp = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise GnaniTransientError(
                f"{where}: {type(exc).__name__}", not_processed=isinstance(exc, _NOT_SENT)
            ) from exc
        if resp.status_code < 400:
            return resp
        code, message = _describe(resp) if internal else (None, "")
        text = f"{where} -> HTTP {resp.status_code}" + (f": {message}" if message else "")
        if resp.status_code == 429:
            # Gnani refuses before doing any work (an assumption Gnani does not document, but a 429 that still
            # created a job would be very unusual), so even Create may be repeated.
            raise GnaniTransientError(text, status_code=429, code=code, not_processed=True)
        if resp.status_code in _SERVER_ERRORS:
            raise GnaniTransientError(text, status_code=resp.status_code, code=code, not_processed=False)
        raise GnaniPermanentError(text, status_code=resp.status_code, code=code)

    def create_batch_job(self, source_url: str, language_code: str) -> str:
        body = {
            "config": {
                "model": BATCH_MODEL,
                "language_code": language_code,
                "mode": "transcribe",
                "with_diarization": False,
                "is_multi_channel": False,
                "with_denoise": False,
            },
            "source": {"type": "cloud_storage", "auth": {"mode": "public"}, "paths": [source_url]},
        }
        try:
            resp = self._call("POST", JOBS_PATH, json=body)
        except GnaniTransientError as exc:
            if exc.not_processed:
                raise
            # A timeout or 5xx may mean Gnani already created the job, and Create has no idempotency key.
            raise GnaniAmbiguousCreateError(
                f"Create Job outcome unknown ({exc}); not retried", status_code=exc.status_code
            ) from exc
        return str(_json(resp, "job_id")["job_id"])

    def start_batch_job(self, job_id: str) -> None:
        try:
            self._call("POST", f"{JOBS_PATH}/{job_id}/start")
        except GnaniPermanentError as exc:
            if exc.status_code != 409:  # 409 = already started, e.g. our first attempt landed but the reply was lost
                raise

    def get_batch_job(self, job_id: str) -> BatchJob:
        data = _json(self._call("GET", f"{JOBS_PATH}/{job_id}"), "status")
        progress = data.get("progress")
        progress = progress if isinstance(progress, dict) else {}
        reason = data.get("cancel_reason")
        return BatchJob(
            status=str(data["status"]),
            cancel_reason=str(reason) if reason else None,
            total_files=_count(progress.get("total_files")),
            completed_files=_count(progress.get("completed_files")),
            failed_files=_count(progress.get("failed_files")),
        )

    def get_batch_files(self, job_id: str) -> list[BatchFile]:
        entries = _json(self._call("GET", f"{JOBS_PATH}/{job_id}/files"), "data")["data"]
        if not isinstance(entries, list):
            raise GnaniProtocolError("unexpected response shape from Gnani (files.data is not a list)")
        return [
            BatchFile(
                file_id=str(entry["file_id"]) if entry.get("file_id") else None,
                status=str(entry.get("status")),
                transcript_url=entry.get("transcript_url") or None,
                error_message=entry.get("error_message") or None,
            )
            for entry in entries
            if isinstance(entry, dict)
        ]

    def download_transcript(self, url: str) -> Transcript:
        data = _json(self._call("GET", url, authenticated=False), "full_transcript")
        duration = data.get("duration_seconds")
        language = data.get("language_code")
        return Transcript(
            text=str(data["full_transcript"] or ""),
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            language_code=str(language) if language else None,
        )


@lru_cache
def get_gnani() -> GnaniApi:
    return GnaniClient.from_settings(get_settings())
