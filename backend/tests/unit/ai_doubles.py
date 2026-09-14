"""Test doubles for the provider SDKs.

Both adapters accept an injected client, so nothing here monkey-patches a
module or opens a socket. The fakes mimic only the attributes the adapters
actually read, which keeps them honest: if an adapter starts depending on
something new, the fake stops satisfying it.

SDK exceptions are constructed for real rather than faked, because the adapters
branch on their concrete classes. Both SDKs build theirs from an ``httpx2``
response, which is already installed as one of their dependencies - so this
needs no test-only package.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx2
import openai

ANTHROPIC_KEY = "test-anthropic-secret"
OPENAI_KEY = "test-openai-secret"


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://provider.invalid/v1/messages")


def _response(status_code: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status_code, headers=headers or {}, request=_request())


# -- Anthropic ----------------------------------------------------------------


def anthropic_message(
    *,
    text: str = "Four refunds are pending.",
    model: str = "claude-opus-5",
    input_tokens: int = 120,
    output_tokens: int = 34,
    message_id: str = "msg_01ABC",
    request_id: str | None = "req_anthropic_123",
    parsed_output: Any = None,
    content: list[Any] | None = None,
) -> SimpleNamespace:
    """A stand-in for ``anthropic.types.Message``."""
    message = SimpleNamespace(
        id=message_id,
        content=content if content is not None else [SimpleNamespace(type="text", text=text)],
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        parsed_output=parsed_output,
    )
    # Public despite the underscore: both SDKs expose the request-id header here.
    message._request_id = request_id
    return message


def anthropic_client(
    *,
    create_result: Any = None,
    create_error: Exception | None = None,
    parse_result: Any = None,
    parse_error: Exception | None = None,
) -> SimpleNamespace:
    """A stand-in for ``anthropic.AsyncAnthropic``."""
    return SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=create_result if create_result is not None else anthropic_message(),
                side_effect=create_error,
            ),
            parse=AsyncMock(
                return_value=parse_result if parse_result is not None else anthropic_message(),
                side_effect=parse_error,
            ),
        )
    )


def anthropic_errors() -> dict[str, Exception]:
    """One real SDK exception of each kind the adapter translates."""
    return {
        "authentication": anthropic.AuthenticationError(
            "invalid x-api-key", response=_response(401), body=None
        ),
        "permission": anthropic.PermissionDeniedError(
            "forbidden", response=_response(403), body=None
        ),
        "rate_limit": anthropic.RateLimitError(
            "rate limited", response=_response(429, {"retry-after": "30"}), body=None
        ),
        "rate_limit_no_header": anthropic.RateLimitError(
            "rate limited", response=_response(429), body=None
        ),
        "timeout": anthropic.APITimeoutError(request=_request()),
        "connection": anthropic.APIConnectionError(request=_request()),
        "not_found": anthropic.NotFoundError("model not found", response=_response(404), body=None),
        "server": anthropic.InternalServerError("overloaded", response=_response(529), body=None),
        "bad_request": anthropic.BadRequestError(
            "invalid request", response=_response(400), body=None
        ),
    }


# -- OpenAI -------------------------------------------------------------------


def openai_completion(
    *,
    text: str | None = "Four refunds are pending.",
    model: str = "gpt-5.5",
    prompt_tokens: int = 120,
    completion_tokens: int = 34,
    completion_id: str = "chatcmpl-01ABC",
    request_id: str | None = "req_openai_123",
    parsed: Any = None,
    choices: list[Any] | None = None,
) -> SimpleNamespace:
    """A stand-in for ``openai.types.chat.ChatCompletion``."""
    if choices is None:
        choices = [SimpleNamespace(message=SimpleNamespace(content=text, parsed=parsed))]

    completion = SimpleNamespace(
        id=completion_id,
        choices=choices,
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )
    completion._request_id = request_id
    return completion


def openai_client(
    *,
    create_result: Any = None,
    create_error: Exception | None = None,
    parse_result: Any = None,
    parse_error: Exception | None = None,
) -> SimpleNamespace:
    """A stand-in for ``openai.AsyncOpenAI``."""
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    return_value=(
                        create_result if create_result is not None else openai_completion()
                    ),
                    side_effect=create_error,
                ),
                parse=AsyncMock(
                    return_value=(
                        parse_result if parse_result is not None else openai_completion()
                    ),
                    side_effect=parse_error,
                ),
            )
        )
    )


def openai_errors() -> dict[str, Exception]:
    """One real SDK exception of each kind the adapter translates."""
    return {
        "authentication": openai.AuthenticationError(
            "invalid api key", response=_response(401), body=None
        ),
        "permission": openai.PermissionDeniedError("forbidden", response=_response(403), body=None),
        "rate_limit": openai.RateLimitError(
            "rate limited", response=_response(429, {"retry-after": "12"}), body=None
        ),
        "rate_limit_no_header": openai.RateLimitError(
            "rate limited", response=_response(429), body=None
        ),
        "timeout": openai.APITimeoutError(request=_request()),
        "connection": openai.APIConnectionError(request=_request()),
        "not_found": openai.NotFoundError("model not found", response=_response(404), body=None),
        "server": openai.InternalServerError("server error", response=_response(500), body=None),
        "bad_request": openai.BadRequestError(
            "invalid request", response=_response(400), body=None
        ),
    }
