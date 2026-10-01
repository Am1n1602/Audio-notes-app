import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.gnani import (
    GnaniAmbiguousCreateError,
    GnaniClient,
    GnaniPermanentError,
    GnaniProtocolError,
    GnaniTransientError,
)

KEY = "gnani-secret-key"
JOB = "job-123"
JOBS = "/stt/v3/batch/jobs"
SIGNED = "https://bucket.s3.test/results/t.json?X-Amz-Signature=super-secret-signature"


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[GnaniClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped), base_url="https://gnani.test")
    return GnaniClient(KEY, "https://gnani.test", http), seen


def answering(status: int, body: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=body if body is not None else {})


def raising(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# --- pacing (Gnani allows about one call per second per key) -------------------------------------------------------


class FakeTime:
    """A clock that only moves when the client 'sleeps', so pacing is tested without waiting."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def paced_client(fake: FakeTime, interval: float = 1.2) -> GnaniClient:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "IN_PROGRESS"})),
        base_url="https://gnani.test",
    )
    return GnaniClient(
        KEY, "https://gnani.test", http, min_call_interval_seconds=interval, clock=fake.clock, sleep=fake.sleep
    )


def test_calls_made_too_close_together_are_spaced_out() -> None:
    fake = FakeTime()
    client = paced_client(fake)
    client.get_batch_job(JOB)  # the first call never waits
    assert fake.slept == []
    fake.now += 0.1  # the next call comes 0.1 s later (the old "get job, then get files" pattern)
    client.get_batch_job(JOB)
    assert fake.slept == [1.1]  # waited just long enough to leave 1.2 s between the two calls


def test_calls_that_are_already_far_enough_apart_do_not_wait() -> None:
    fake = FakeTime()
    client = paced_client(fake)
    client.get_batch_job(JOB)
    fake.now += 5
    client.get_batch_job(JOB)
    assert fake.slept == []


def test_the_wait_counts_from_when_the_previous_call_ended_not_when_it_started() -> None:
    """Gnani counts a request when it arrives. Counting from the previous call's START let a slow first call be followed
    by a second that arrived under a second later (a real 'get job' then 'get files' were 0.8 s apart: a 429)."""
    fake = FakeTime()

    def slow(_request: httpx.Request) -> httpx.Response:
        fake.now += 0.9  # the call itself takes 0.9 s
        return httpx.Response(200, json={"status": "IN_PROGRESS"})

    http = httpx.Client(transport=httpx.MockTransport(slow), base_url="https://gnani.test")
    client = GnaniClient(
        KEY, "https://gnani.test", http, min_call_interval_seconds=1.2, clock=fake.clock, sleep=fake.sleep
    )
    client.get_batch_job(JOB)
    client.get_batch_job(JOB)  # wanted right after the first one ended
    assert fake.slept == [1.2]  # a full interval after it ended (from its start it would have been only 0.3 s)


def test_a_call_that_fails_still_counts_for_pacing() -> None:
    fake = FakeTime()
    http = httpx.Client(
        transport=httpx.MockTransport(raising(httpx.ReadTimeout("slow"))), base_url="https://gnani.test"
    )
    client = GnaniClient(
        KEY, "https://gnani.test", http, min_call_interval_seconds=1.2, clock=fake.clock, sleep=fake.sleep
    )
    for _ in range(2):
        with pytest.raises(GnaniTransientError):
            client.get_batch_job(JOB)
    assert fake.slept == [1.2]


def test_the_transcript_download_is_not_paced_because_it_is_s3_not_gnani() -> None:
    fake = FakeTime()
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"full_transcript": "hi", "status": "X"})),
        base_url="https://gnani.test",
    )
    client = GnaniClient(
        KEY, "https://gnani.test", http, min_call_interval_seconds=1.2, clock=fake.clock, sleep=fake.sleep
    )
    client.download_transcript(SIGNED)
    client.download_transcript(SIGNED)
    assert fake.slept == []


def test_pacing_can_be_switched_off() -> None:
    fake = FakeTime()
    client = paced_client(fake, interval=0)
    client.get_batch_job(JOB)
    client.get_batch_job(JOB)
    assert fake.slept == []


# --- create --------------------------------------------------------------------------------------------------


def test_create_sends_the_documented_request_and_returns_the_job_id() -> None:
    client, seen = client_with(answering(201, {"job_id": JOB, "status": "CREATED"}))
    assert client.create_batch_job("https://bucket.test/a.wav?sig=1", "hi-IN,en-IN") == JOB
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", JOBS)
    assert request.headers["X-API-Key-ID"] == KEY
    assert json.loads(request.read()) == {
        "config": {
            "model": "gnani-prisma-v2.5",
            "language_code": "hi-IN,en-IN",
            "mode": "transcribe",
            "with_diarization": False,
            "is_multi_channel": False,
            "with_denoise": False,
        },
        "source": {"type": "cloud_storage", "auth": {"mode": "public"}, "paths": ["https://bucket.test/a.wav?sig=1"]},
    }


@pytest.mark.parametrize(
    "handler",
    [
        answering(500, {"error": "X", "message": "boom"}),
        answering(503),
        answering(502),
        answering(504),
        raising(httpx.ReadTimeout("slow")),
        raising(httpx.RemoteProtocolError("dropped")),
    ],
)
def test_create_with_an_unknown_outcome_is_ambiguous_and_never_a_plain_retry(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client, seen = client_with(handler)
    with pytest.raises(GnaniAmbiguousCreateError, match="outcome unknown"):
        client.create_batch_job("https://bucket.test/a.wav", "en-IN")
    assert len(seen) <= 1  # the client itself never retries


@pytest.mark.parametrize(
    "handler",
    [answering(429, {"error": "RATE_LIMITED", "message": "slow down"}), raising(httpx.ConnectError("refused"))],
)
def test_create_that_provably_never_reached_gnani_is_a_safe_transient_error(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client, _ = client_with(handler)
    with pytest.raises(GnaniTransientError) as info:
        client.create_batch_job("https://bucket.test/a.wav", "en-IN")
    assert info.value.not_processed is True


def test_create_400_and_the_auth_error_shape_are_permanent_with_gnanis_own_code() -> None:
    bad = {"error": "UNSUPPORTED_LANGUAGE", "message": "no xx-XX"}
    client, _ = client_with(answering(400, bad))
    with pytest.raises(GnaniPermanentError) as info:
        client.create_batch_job("https://bucket.test/a.wav", "xx-XX")
    assert (info.value.status_code, info.value.code) == (400, "UNSUPPORTED_LANGUAGE")
    assert "no xx-XX" in str(info.value)

    denied = {"detail": {"error_code": "INVALID_API_KEY", "message": "Unauthorized", "status_code": 401}}
    client, _ = client_with(answering(401, denied))
    with pytest.raises(GnaniPermanentError) as info:
        client.create_batch_job("https://bucket.test/a.wav", "en-IN")
    assert (info.value.status_code, info.value.code) == (401, "INVALID_API_KEY")


def test_create_response_without_a_job_id_is_a_protocol_error() -> None:
    client, _ = client_with(answering(201, {"status": "CREATED"}))
    with pytest.raises(GnaniProtocolError):
        client.create_batch_job("https://bucket.test/a.wav", "en-IN")


# --- start / status / files ---------------------------------------------------------------------------------


def test_start_is_idempotent_because_409_means_already_started() -> None:
    client, seen = client_with(answering(202, {"job_id": JOB, "status": "STARTING"}))
    client.start_batch_job(JOB)
    assert (seen[0].method, seen[0].url.path) == ("POST", f"{JOBS}/{JOB}/start")

    already = {"error": "JOB_ALREADY_STARTED", "message": "Job cannot be restarted from state 'FAILED'"}
    client, _ = client_with(answering(409, already))
    client.start_batch_job(JOB)  # no exception


def test_start_still_raises_for_real_errors() -> None:
    client, _ = client_with(answering(404, {"error": "JOB_NOT_FOUND", "message": "gone"}))
    with pytest.raises(GnaniPermanentError):
        client.start_batch_job(JOB)
    client, _ = client_with(answering(429, {"error": "RATE_LIMITED", "message": "x"}))
    with pytest.raises(GnaniTransientError):
        client.start_batch_job(JOB)


def test_get_job_parses_status_and_progress() -> None:
    body = {
        "job_id": JOB,
        "status": "IN_PROGRESS",
        "cancel_reason": None,
        "progress": {"total_files": 1, "completed_files": 0, "failed_files": 0, "percent": 40},
    }
    client, seen = client_with(answering(200, body))
    job = client.get_batch_job(JOB)
    assert (job.status, job.cancel_reason, job.total_files, job.completed_files) == ("IN_PROGRESS", None, 1, 0)
    assert seen[0].headers["X-API-Key-ID"] == KEY


def test_get_job_keeps_the_reason_a_job_never_started() -> None:
    body = {"status": "START_FAILED", "cancel_reason": "All provided paths were invalid — nothing to process."}
    client, _ = client_with(answering(200, body))
    assert "invalid" in (client.get_batch_job(JOB).cancel_reason or "")


def test_get_job_server_error_is_transient_but_not_provably_unprocessed() -> None:
    client, _ = client_with(answering(503))
    with pytest.raises(GnaniTransientError) as info:
        client.get_batch_job(JOB)
    assert info.value.not_processed is False


def test_get_files_parses_completed_and_failed_files() -> None:
    body = {
        "data": [
            {"file_id": "f1", "status": "COMPLETED", "transcript_url": SIGNED, "error_message": None},
            {"file_id": "f2", "status": "FAILED", "transcript_url": None, "error_message": "public download: HTTP 406"},
        ]
    }
    client, _ = client_with(answering(200, body))
    ok, failed = client.get_batch_files(JOB)
    assert (ok.status, ok.transcript_url, ok.error_message) == ("COMPLETED", SIGNED, None)
    assert (failed.status, failed.transcript_url, failed.error_message) == ("FAILED", None, "public download: HTTP 406")


# --- transcript download -------------------------------------------------------------------------------------


def test_transcript_download_never_sends_the_api_key_and_parses_the_text() -> None:
    body = {"full_transcript": "hello world", "duration_seconds": 6.01, "language_code": "en-IN", "segments": []}
    client, seen = client_with(answering(200, body))
    transcript = client.download_transcript(SIGNED)
    assert (transcript.text, transcript.duration_seconds, transcript.language_code) == ("hello world", 6.01, "en-IN")
    assert "X-API-Key-ID" not in seen[0].headers  # the link is a presigned S3 URL, not Gnani


def test_transcript_download_failure_reveals_neither_the_signed_url_nor_the_key() -> None:
    client, _ = client_with(answering(403, {}))
    with pytest.raises(GnaniPermanentError) as info:
        client.download_transcript(SIGNED)
    text = str(info.value)
    assert "super-secret-signature" not in text and "s3.test" not in text and KEY not in text
    assert "external url" in text


def test_transcript_without_full_transcript_is_a_protocol_error() -> None:
    client, _ = client_with(answering(200, {"segments": []}))
    with pytest.raises(GnaniProtocolError):
        client.download_transcript(SIGNED)


def test_a_null_transcript_becomes_an_empty_string() -> None:
    client, _ = client_with(answering(200, {"full_transcript": None}))
    assert client.download_transcript(SIGNED).text == ""


def test_transport_errors_never_leak_exception_text() -> None:
    client, _ = client_with(raising(httpx.ConnectError(f"cannot connect to {SIGNED}")))
    with pytest.raises(GnaniTransientError) as info:
        client.download_transcript(SIGNED)
    assert "super-secret-signature" not in str(info.value)
