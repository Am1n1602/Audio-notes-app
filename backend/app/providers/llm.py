"""Summary LLM client for Groq's OpenAI-compatible chat completions API: the only module that knows the vendor.

Like the Gnani client it raises typed errors and NEVER retries or sleeps; the retry policy lives in
services/summarization.py, where it can be tested without waiting. Any OpenAI-compatible service works by changing
LLM_BASE_URL and LLM_MODEL.

Error types (what the caller may safely do next):
  LlmTransientError   worth retrying: network failure, 429, 5xx. For a 429, `retry_after` is the number of seconds
                      Groq asked us to wait (its `retry-after` header), if it sent one.
  LlmPermanentError   retrying the same request cannot help (bad request, bad key, request too large, ...).
  LlmProtocolError    an answer that is unusable: wrong shape, empty, or cut off at the token limit. In JSON mode Groq
                      reports a cut-off or malformed answer as HTTP 400 `json_validate_failed` (observed live), so that
                      one 400 is an unusable answer, not a rejected request.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core.config import Settings, get_settings, require_secret

CHAT_PATH = "/chat/completions"
_SERVER_ERRORS = frozenset({500, 502, 503, 504})
JSON_FAILED = "json_validate_failed"


class LlmError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LlmTransientError(LlmError):
    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class LlmPermanentError(LlmError):
    pass


class LlmProtocolError(LlmPermanentError):
    pass


class LlmTruncatedError(LlmProtocolError):
    """The answer was cut off at max_completion_tokens (Groq: finish_reason "length", or HTTP 400 json_validate_failed).
    Asking again for the same thing would be cut off in the same place, so the caller asks for a shorter answer."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int | None
    completion_tokens: int | None


class SummaryLlm(Protocol):
    """What the worker needs from the LLM. GroqClient is the real one; tests use a scripted fake."""

    def complete_json(self, system: str, user: str) -> ChatResult:
        """One chat completion in JSON mode. Returns the raw message text (the caller parses and validates it)."""
        ...


def _describe(resp: httpx.Response) -> str:
    """OpenAI-style error bodies look like {"error": {"message": ..., "type": ..., "code": ...}}."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = body["error"]
        text = " - ".join(str(part) for part in (error.get("code") or error.get("type"), error.get("message")) if part)
        if text:
            return text[:300]
    return (resp.text[:200] or resp.reason_phrase) if resp.text else resp.reason_phrase


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None  # an HTTP-date form: not used by Groq, and not worth parsing


def _count(value: object) -> int | None:
    return value if isinstance(value, int) else None


class GroqClient:
    def __init__(
        self, api_key: str, base_url: str, model: str, max_output_tokens: int, http: httpx.Client | None = None
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._http = http or httpx.Client(
            base_url=base_url, timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5)
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "GroqClient":
        return cls(
            require_secret(settings.llm_api_key, "LLM_API_KEY"),
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_max_output_tokens,
        )

    def complete_json(self, system: str, user: str) -> ChatResult:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Low but not zero: at exactly 0 a retry after a malformed answer would return the same malformed answer.
            "temperature": 0.2,
            "max_completion_tokens": self._max_output_tokens,  # `max_tokens` is deprecated
            "response_format": {"type": "json_object"},  # valid JSON guaranteed; the prompt must ask for JSON
        }
        where = f"POST {CHAT_PATH}"
        try:
            resp = self._http.post(CHAT_PATH, json=body, headers={"Authorization": f"Bearer {self._api_key}"})
        except httpx.TransportError as exc:
            raise LlmTransientError(f"{where}: {type(exc).__name__}") from exc  # never str(exc): keep text minimal

        if resp.status_code >= 400:
            text = f"{where} -> HTTP {resp.status_code}: {_describe(resp)}"
            if resp.status_code == 429:
                raise LlmTransientError(text, status_code=429, retry_after=_retry_after(resp))
            if resp.status_code in _SERVER_ERRORS:
                raise LlmTransientError(text, status_code=resp.status_code)
            if resp.status_code == 400 and _error_code(resp) == JSON_FAILED:
                raise LlmTruncatedError(text, status_code=400)
            raise LlmPermanentError(text, status_code=resp.status_code)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: httpx.Response) -> ChatResult:
        try:
            data: Any = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmProtocolError("unexpected response shape from the LLM") from exc
        if not isinstance(content, str) or not content.strip():
            raise LlmProtocolError("the LLM returned an empty message")
        if finish_reason == "length":
            raise LlmTruncatedError("the LLM's answer was cut off at the token limit")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            content=content,
            prompt_tokens=_count(usage.get("prompt_tokens")),
            completion_tokens=_count(usage.get("completion_tokens")),
        )


@lru_cache
def get_llm() -> SummaryLlm:
    return GroqClient.from_settings(get_settings())
