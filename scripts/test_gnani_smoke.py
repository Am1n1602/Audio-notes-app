"""Mocked checks for gnani_smoke_test.py. Run: python scripts/test_gnani_smoke.py  (or pytest)."""

import json
import tempfile
from pathlib import Path

import gnani_smoke_test as gs
import httpx

KEY = "test-key"
JOB = "job-1"
JOBS = "/stt/v3/batch/jobs"
TRANSCRIPT_URL = "https://s3.test/results/t.json?X-Amz-Signature=secret"

SLEPT: list[float] = []
gs.time.sleep = SLEPT.append  # no real waiting; also lets us assert poll/backoff delays


def fake(script):
    """script: {(method, path): [(status, json) | exception, ...]}; consumed in order, the last repeats."""
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        queue = script[(req.method, req.url.path)]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    SLEPT.clear()
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.test"), calls


def happy(overrides=None):
    script = {
        ("POST", JOBS): [(201, {"job_id": JOB, "status": "CREATED"})],
        ("POST", f"{JOBS}/{JOB}/start"): [(202, {"job_id": JOB, "status": "STARTING"})],
        ("GET", f"{JOBS}/{JOB}"): [(200, {"status": "IN_PROGRESS"}), (200, {"status": "COMPLETED"})],
        ("GET", f"{JOBS}/{JOB}/files"): [
            (200, {"data": [{"file_id": "f1", "status": "COMPLETED", "transcript_url": TRANSCRIPT_URL}]})
        ],
        ("GET", "/results/t.json"): [(200, {"full_transcript": "hello world", "duration_seconds": 6.01})],
    }
    script.update(overrides or {})
    return script


def expect_error(fn, *needles):
    try:
        fn()
    except gs.GnaniError as exc:
        assert all(n in str(exc) for n in needles), str(exc)
        return exc
    raise AssertionError("expected GnaniError")


def test_happy_path_url_source():
    client, calls = fake(happy())
    result = gs.run(client, KEY, "https://bucket.test/a.mp3?sig=1", "hi-IN")
    assert result["errors"] == []
    assert result["transcripts"][0]["transcript"]["full_transcript"] == "hello world"
    assert SLEPT == [10]  # one wait between the IN_PROGRESS and COMPLETED polls
    create = calls[0]
    assert create.headers["X-API-Key-ID"] == KEY
    assert json.loads(create.read()) == {
        "config": {
            "model": "gnani-prisma-v2.5",
            "language_code": "hi-IN",
            "mode": "transcribe",
            "with_diarization": False,
            "is_multi_channel": False,
            "with_denoise": False,
        },
        "source": {
            "type": "cloud_storage",
            "auth": {"mode": "public"},
            "paths": ["https://bucket.test/a.mp3?sig=1"],
        },
    }
    # the presigned transcript URL must never receive the Gnani API key
    assert all(("X-API-Key-ID" in c.headers) == (c.url.host == "api.test") for c in calls)


def test_local_file_is_sent_as_multipart():
    client, calls = fake(happy())
    with tempfile.TemporaryDirectory() as d:
        clip = Path(d, "clip.mp3")
        clip.write_bytes(b"ID3fakeaudio")
        gs.run(client, KEY, str(clip))
    body = calls[0].read().decode("latin-1")
    assert calls[0].headers["content-type"].startswith("multipart/form-data")
    assert 'name="config"\r\nContent-Type: application/json' in body  # no filename on the config part
    assert 'name="files"; filename="clip.mp3"' in body and "ID3fakeaudio" in body


def test_transient_failures_are_retried_then_succeed():
    client, calls = fake(
        happy(
            {
                ("POST", JOBS): [
                    (429, {"error": "RATE_LIMITED", "message": "slow down"}),
                    (429, {}),
                    (201, {"job_id": JOB}),
                ],
                ("GET", f"{JOBS}/{JOB}"): [(503, {}), (200, {"status": "COMPLETED"})],
            }
        )
    )
    assert gs.run(client, KEY, "https://bucket.test/a.mp3")["transcripts"]
    assert len([c for c in calls if c.method == "POST" and c.url.path == JOBS]) == 3
    assert len(SLEPT) == 3 and SLEPT[0] < SLEPT[1]  # 2 create backoffs (exponential) + 1 status backoff


def test_retries_are_bounded():
    client, calls = fake(happy({("POST", f"{JOBS}/{JOB}/start"): [(503, {})]}))
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "5 attempts")
    assert len([c for c in calls if c.url.path.endswith("/start")]) == 5


def test_create_is_not_retried_when_the_outcome_is_unknown():
    # A 5xx or a timeout may mean Gnani already created the job; retrying could duplicate it.
    for failure in ([(503, {})], [(500, {})], [httpx.ReadTimeout("slow")]):
        client, calls = fake(happy({("POST", JOBS): failure}))
        exc = expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "Cannot tell", "orphan")
        assert isinstance(exc, gs.AmbiguousCreateError) and len(calls) == 1 and SLEPT == []


def test_create_is_retried_when_the_request_never_reached_gnani():
    client, calls = fake(happy({("POST", JOBS): [httpx.ConnectError("refused"), (201, {"job_id": JOB})]}))
    assert gs.run(client, KEY, "https://bucket.test/a.mp3")["transcripts"]
    assert len([c for c in calls if c.method == "POST" and c.url.path == JOBS]) == 2


def test_permanent_400_is_not_retried():
    bad = {"error": "UNSUPPORTED_LANGUAGE", "message": "no xx-XX"}
    client, calls = fake(happy({("POST", JOBS): [(400, bad)]}))
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3", "xx-XX"), "UNSUPPORTED_LANGUAGE", "no xx-XX")
    assert len(calls) == 1 and SLEPT == []


def test_auth_failure_shows_detail_shape_and_hint():
    detail = {"detail": {"error_code": "MISSING_API_KEY", "message": "Missing API key", "status_code": 401}}
    client, calls = fake(happy({("POST", JOBS): [(401, detail)]}))
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "MISSING_API_KEY", "GNANI_API_KEY")
    assert len(calls) == 1


def test_start_409_falls_through_to_status():
    client, _ = fake(
        happy({("POST", f"{JOBS}/{JOB}/start"): [(409, {"error": "JOB_ALREADY_STARTED", "message": "x"})]})
    )
    assert gs.run(client, KEY, "https://bucket.test/a.mp3")["transcripts"]


def test_failed_file_and_empty_transcript_are_reported():
    files = {
        "data": [
            {"file_id": "f1", "status": "FAILED", "error_message": "Empty transcript after 3 retries"},
            {"file_id": "f2", "status": "COMPLETED", "transcript_url": TRANSCRIPT_URL},
        ]
    }
    client, _ = fake(
        happy(
            {
                ("GET", f"{JOBS}/{JOB}/files"): [(200, files)],
                ("GET", "/results/t.json"): [(200, {"full_transcript": "  "})],
            }
        )
    )
    result = gs.run(client, KEY, "https://bucket.test/a.mp3")
    assert result["transcripts"] == []
    assert "Empty transcript after 3 retries" in result["errors"][0] and "empty transcript" in result["errors"][1]


def test_terminal_failure_and_poll_timeout():
    failed = {"status": "START_FAILED", "cancel_reason": "All provided paths were invalid"}
    client, _ = fake(
        happy({("GET", f"{JOBS}/{JOB}"): [(200, failed)], ("GET", f"{JOBS}/{JOB}/files"): [(200, {"data": []})]})
    )
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "START_FAILED", "invalid")
    client, _ = fake(happy())
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3", timeout_seconds=0), JOB, "keeps running")


def test_failed_job_reports_the_per_file_reason():
    # Real Gnani behaviour (seen live): job FAILED, cancel_reason null, cause only in files[].error_message.
    files = {"data": [{"file_id": "f1", "status": "FAILED", "error_message": "public download: HTTP 406"}]}
    client, _ = fake(
        happy(
            {
                ("GET", f"{JOBS}/{JOB}"): [(200, {"status": "FAILED", "cancel_reason": None})],
                ("GET", f"{JOBS}/{JOB}/files"): [(200, files)],
            }
        )
    )
    expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "FAILED", "public download: HTTP 406")


def test_bad_sources_fail_before_any_request():
    client, calls = fake(happy())
    with tempfile.TemporaryDirectory() as d:
        Path(d, "notes.txt").write_bytes(b"x")
        Path(d, "big.wav").write_bytes(b"0" * (gs.MAX_UPLOAD_BYTES + 1))
        for source, needle in [
            (str(Path(d, "notes.txt")), "unsupported extension"),
            (str(Path(d, "big.wav")), "10 MB"),
            (str(Path(d, "missing.wav")), "not found"),
            ("http://bucket.test/a.mp3", "https"),
            ("https://bucket.test/folder/?sig=1", "folder"),
        ]:
            expect_error(lambda: gs.run(client, KEY, source), needle)
    assert calls == []


def test_error_messages_do_not_leak_signed_urls():
    client, _ = fake(happy({("GET", "/results/t.json"): [(403, {})]}))
    exc = expect_error(lambda: gs.run(client, KEY, "https://bucket.test/a.mp3"), "external url")
    assert "Signature" not in str(exc) and "s3.test" not in str(exc)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
