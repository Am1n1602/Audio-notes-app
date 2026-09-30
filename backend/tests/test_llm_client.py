import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.providers.llm import GroqClient, LlmPermanentError, LlmProtocolError, LlmTransientError, LlmTruncatedError

KEY = "gsk-secret-key-value"
GOOD = {
    "choices": [{"message": {"role": "assistant", "content": '{"overview": "x"}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 35, "total_tokens": 155},
}


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[GroqClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped), base_url="https://llm.test/openai/v1")
    return GroqClient(KEY, "https://llm.test/openai/v1", "some-model", 900, http), seen


def answering(status: int, body: Any = None, headers: dict[str, str] | None = None) -> Callable[..., httpx.Response]:
    return lambda _r: httpx.Response(status, json=body if body is not None else {}, headers=headers)


def test_the_request_uses_the_documented_groq_fields() -> None:
    client, seen = client_with(answering(200, GOOD))
    client.complete_json("SYSTEM PROMPT", "USER PROMPT")
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/openai/v1/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.read()) == {
        "model": "some-model",
        "messages": [
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "USER PROMPT"},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 900,  # not the deprecated max_tokens
        "response_format": {"type": "json_object"},
    }


def test_the_answer_and_token_usage_are_returned() -> None:
    client, _ = client_with(answering(200, GOOD))
    result = client.complete_json("s", "u")
    assert (result.content, result.prompt_tokens, result.completion_tokens) == ('{"overview": "x"}', 120, 35)


def test_missing_usage_is_fine() -> None:
    body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    client, _ = client_with(answering(200, body))
    assert client.complete_json("s", "u").prompt_tokens is None


# --- rate limits and outages are transient -----------------------------------------------------------------------


def test_a_rate_limit_carries_how_long_groq_asked_us_to_wait() -> None:
    error = {
        "error": {"message": "Rate limit reached ... try again in 7s", "type": "tokens", "code": "rate_limit_exceeded"}
    }
    client, _ = client_with(answering(429, error, {"retry-after": "7"}))
    with pytest.raises(LlmTransientError) as info:
        client.complete_json("s", "u")
    assert (info.value.status_code, info.value.retry_after) == (429, 7.0)
    assert "rate_limit_exceeded" in str(info.value)


def test_a_rate_limit_without_a_retry_after_header_is_still_transient() -> None:
    client, _ = client_with(answering(429, {}))
    with pytest.raises(LlmTransientError) as info:
        client.complete_json("s", "u")
    assert info.value.retry_after is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_transient(status: int) -> None:
    client, _ = client_with(answering(status, {}))
    with pytest.raises(LlmTransientError):
        client.complete_json("s", "u")


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x")])
def test_network_failures_are_transient(exc: Exception) -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise exc

    client, _ = client_with(boom)
    with pytest.raises(LlmTransientError):
        client.complete_json("s", "u")


# --- everything else is permanent --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_permanent_and_never_retried_by_the_client(status: int) -> None:
    error = {"error": {"message": "Invalid API Key", "type": "invalid_request_error", "code": "invalid_api_key"}}
    client, seen = client_with(answering(status, error))
    with pytest.raises(LlmPermanentError) as info:
        client.complete_json("s", "u")
    assert info.value.status_code == status and "invalid_api_key" in str(info.value)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
    ],
)
def test_an_unusable_success_response_is_a_protocol_error(body: dict[str, Any]) -> None:
    client, _ = client_with(answering(200, body))
    with pytest.raises(LlmProtocolError) as info:
        client.complete_json("s", "u")
    assert not isinstance(info.value, LlmTruncatedError)  # malformed is not the same as cut off


def test_an_answer_cut_off_at_the_token_limit_is_a_protocol_error() -> None:
    body = {"choices": [{"message": {"content": '{"overview": "half a sent'}, "finish_reason": "length"}]}
    client, _ = client_with(answering(200, body))
    with pytest.raises(LlmTruncatedError, match="cut off"):  # its own type: the caller asks for a shorter answer
        client.complete_json("s", "u")


def test_groqs_json_mode_failure_is_an_unusable_answer_not_a_rejected_request() -> None:
    """Observed live: a JSON-mode answer cut off at max_completion_tokens comes back as HTTP 400, not 200."""
    error = {
        "error": {
            "message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
            "type": "invalid_request_error",
            "code": "json_validate_failed",
            "failed_generation": "the transcript said: my secret plans",
        }
    }
    client, seen = client_with(answering(400, error))
    with pytest.raises(LlmTruncatedError) as info:
        client.complete_json("s", "u")
    assert "json_validate_failed" in str(info.value) and len(seen) == 1
    assert "secret plans" not in str(info.value)  # failed_generation can echo the transcript: never surfaced


def test_other_400s_stay_permanent() -> None:
    error = {"error": {"message": "bad field", "type": "invalid_request_error", "code": "invalid_request"}}
    client, _ = client_with(answering(400, error))
    with pytest.raises(LlmPermanentError) as info:
        client.complete_json("s", "u")
    assert not isinstance(info.value, LlmProtocolError)


def test_a_stalled_connection_is_given_up_on_after_thirty_seconds() -> None:
    """A step must finish inside Redis' visibility timeout (see test_step_chain), which bounds the read timeout."""
    timeout = GroqClient(KEY, "https://llm.test", "m", 900)._http.timeout
    assert (timeout.connect, timeout.read) == (5, 30)


def test_errors_never_reveal_the_api_key() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, Authorization: Bearer {KEY}")

    client, _ = client_with(boom)
    with pytest.raises(LlmTransientError) as info:
        client.complete_json("s", "u")
    assert KEY not in str(info.value)
    client, _ = client_with(answering(401, {"error": {"message": "bad key", "code": "invalid_api_key"}}))
    with pytest.raises(LlmPermanentError) as info2:
        client.complete_json("s", "u")
    assert KEY not in str(info2.value)
