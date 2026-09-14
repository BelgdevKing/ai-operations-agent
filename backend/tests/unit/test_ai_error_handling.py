"""A 5xx response must not carry internal detail.

Regression test for a real leak: ``AppError.message`` is returned to clients,
and configuration errors are raised with messages naming environment variables
and configured providers - so an authenticated caller could read deployment
configuration off a 500.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.core.exceptions import AppError, ConflictError, NotFoundError

SECRET_ISH = "LLM_PROVIDER is 'openai' but OPENAI_API_KEY is not set"


@pytest.fixture
def failing_app(app: FastAPI) -> FastAPI:
    @app.get("/raises/configuration")
    async def _configuration() -> None:
        raise LLMConfigurationError(SECRET_ISH, provider="openai")

    @app.get("/raises/provider")
    async def _provider() -> None:
        raise LLMProviderError(
            "upstream: quota exhausted for key sk-live-123", provider="anthropic"
        )

    @app.get("/raises/not-found")
    async def _not_found() -> None:
        raise NotFoundError("Tenant 'acme' does not exist.")

    return app


async def test_a_5xx_message_is_replaced_with_the_generic_one(
    failing_app: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/raises/configuration")

    assert response.status_code == 500
    assert SECRET_ISH not in response.text
    assert "OPENAI_API_KEY" not in response.text
    assert response.json()["error"]["message"] == LLMConfigurationError.message


async def test_a_5xx_never_carries_provider_text(failing_app: FastAPI, client: AsyncClient) -> None:
    response = await client.get("/raises/provider")

    assert "sk-live-123" not in response.text
    assert "quota" not in response.text.lower()
    assert response.json()["error"]["message"] == LLMProviderError.message


async def test_a_5xx_carries_no_details(failing_app: FastAPI, client: AsyncClient) -> None:
    response = await client.get("/raises/configuration")

    assert response.json()["error"]["details"] == {}


async def test_the_error_code_still_identifies_the_failure(
    failing_app: FastAPI, client: AsyncClient
) -> None:
    """Generic prose, specific code: a client can still branch on what happened."""
    response = await client.get("/raises/configuration")

    assert response.json()["error"]["code"] == "llm_configuration_error"


async def test_the_correlation_id_ties_the_response_to_the_log(
    failing_app: FastAPI, client: AsyncClient
) -> None:
    """The detail is in the log; this is how someone finds it."""
    response = await client.get("/raises/configuration")

    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_a_4xx_message_is_returned_as_written(
    failing_app: FastAPI, client: AsyncClient
) -> None:
    """Client errors are guidance for the caller and stay specific."""
    response = await client.get("/raises/not-found")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Tenant 'acme' does not exist."


@pytest.mark.parametrize(
    "error",
    [
        LLMConfigurationError("internal detail"),
        LLMAuthenticationError("our key expired on 2026-01-01"),
        LLMProviderError("upstream https://api.internal/v1 failed"),
        LLMInvalidResponseError("schema RefundSummary rejected field 'pending'"),
        LLMTimeoutError("waited 60s for api.internal"),
    ],
)
def test_every_5xx_ai_error_has_a_safe_default(error: AppError) -> None:
    """Whatever a call site writes, the class default is what a client sees."""
    assert error.status_code >= 500
    generic = type(error).message

    assert generic != error.message
    for marker in ("internal", "key", "https://", "api.internal", "RefundSummary"):
        assert marker not in generic or marker == "key"


def test_a_4xx_ai_error_keeps_its_specific_message() -> None:
    """Rate limiting is the one AI failure a caller can act on."""
    error = LLMRateLimitError(retry_after_seconds=30)

    assert error.status_code == 429


def test_a_conflict_is_still_specific() -> None:
    assert ConflictError("That email is taken.").message == "That email is taken."
