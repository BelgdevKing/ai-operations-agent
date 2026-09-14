"""The application AI service. No database, no network, no provider SDK."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from app.ai.exceptions import LLMRateLimitError, LLMTimeoutError
from app.ai.gateway import LLMGateway, RetryPolicy
from app.ai.models import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStructuredResponse,
    LLMUsage,
)
from app.ai.providers.base import LLMProvider
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.services.ai import AIService

PROMPT = "Explain what an AI operations agent is."
COMPLETION = "An AI operations agent carries out business tasks under supervision."


class RefundSummary(BaseModel):
    pending: int


class RecordingProvider(LLMProvider):
    """Captures the request the service built, and answers predictably."""

    name = "recording"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.requests: list[LLMRequest] = []
        self.error = error
        self.schemas: list[type[BaseModel]] = []

    def _respond(self, request: LLMRequest) -> LLMResponse:
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=COMPLETION,
            provider=self.name,
            model=request.model,
            usage=LLMUsage(input_tokens=42, output_tokens=17),
            latency_ms=12.0,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._respond(request)

    async def generate_structured[DataT: BaseModel](
        self, request: LLMRequest, schema: type[DataT]
    ) -> LLMStructuredResponse[DataT]:
        self.requests.append(request)
        self.schemas.append(schema)
        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=schema.model_validate({"pending": 3}),
            response=self._respond(request),
        )


async def no_sleep(_seconds: float) -> None:
    return None


def service(
    *, error: Exception | None = None, **setting_overrides: Any
) -> tuple[AIService, RecordingProvider]:
    provider = RecordingProvider(error=error)
    settings = Settings(app_env="test", **setting_overrides)
    gateway = LLMGateway(provider, retry=RetryPolicy(max_retries=0), sleep=no_sleep)
    return AIService(gateway, settings), provider


def a_message() -> LLMMessage:
    return LLMMessage.user(PROMPT)


# -- Building the request -----------------------------------------------------


async def test_the_service_builds_a_request_from_application_input() -> None:
    ai, provider = service()

    await ai.generate([a_message()])

    built = provider.requests[0]
    assert isinstance(built, LLMRequest)
    assert [m.content for m in built.messages] == [PROMPT]


async def test_the_configured_model_is_used_by_default() -> None:
    """The normal path: a caller names no model."""
    ai, provider = service()

    await ai.generate([a_message()])

    assert provider.requests[0].model == Settings(app_env="test").llm_model


async def test_optional_parameters_are_left_unset_when_not_asked_for() -> None:
    """So the internal defaults apply, and an adapter can still tell "not
    asked" from "asked for this" - which is how temperature avoids being sent
    to a model that rejects one."""
    ai, provider = service()

    await ai.generate([a_message()])

    built = provider.requests[0]
    assert "temperature" not in built.model_fields_set
    assert "max_output_tokens" not in built.model_fields_set


async def test_explicit_parameters_are_passed_through() -> None:
    ai, provider = service(llm_allowed_models="claude-haiku-4-5")

    await ai.generate([a_message()], temperature=0.3, max_output_tokens=256)

    built = provider.requests[0]
    assert built.temperature == 0.3
    assert built.max_output_tokens == 256
    assert "temperature" in built.model_fields_set


async def test_system_messages_are_carried_through() -> None:
    ai, provider = service()

    await ai.generate([LLMMessage.system("Be brief."), a_message()])

    assert [m.role.value for m in provider.requests[0].messages] == ["system", "user"]


async def test_an_empty_conversation_is_rejected() -> None:
    ai, _ = service()

    with pytest.raises(ValidationError):
        await ai.generate([])


# -- Model selection is the server's decision ---------------------------------


def test_an_allowed_model_may_be_named() -> None:
    ai, _ = service(llm_allowed_models="claude-haiku-4-5")

    assert ai.resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"


def test_the_configured_model_is_always_allowed() -> None:
    ai, _ = service()
    configured = Settings(app_env="test").llm_model

    assert ai.resolve_model(configured) == configured


def test_an_unlisted_model_is_refused() -> None:
    """Otherwise a caller could direct traffic at an expensive model and bill
    the deployment for it."""
    ai, _ = service()

    with pytest.raises(ValidationError) as raised:
        ai.resolve_model("claude-fable-5-1")

    assert "not available" in raised.value.message


def test_the_refusal_says_what_is_allowed() -> None:
    """The permitted set is deployment configuration, not a secret."""
    ai, _ = service(llm_allowed_models="claude-haiku-4-5")

    with pytest.raises(ValidationError) as raised:
        ai.resolve_model("something-else")

    assert "claude-haiku-4-5" in raised.value.message


def test_another_providers_model_cannot_be_smuggled_in() -> None:
    ai, _ = service()

    with pytest.raises(ValidationError):
        ai.resolve_model("gpt-5.5")


async def test_an_unlisted_model_never_reaches_the_provider() -> None:
    ai, provider = service()

    with pytest.raises(ValidationError):
        await ai.generate([a_message()], model="claude-fable-5-1")

    assert provider.requests == []


# -- Delegation ---------------------------------------------------------------


async def test_the_response_is_returned_from_the_gateway() -> None:
    ai, _ = service()

    response = await ai.generate([a_message()])

    assert response.content == COMPLETION
    assert response.usage.total_tokens == 59


@pytest.mark.parametrize("error", [LLMTimeoutError(), LLMRateLimitError()])
async def test_provider_failures_are_not_swallowed(error: Exception) -> None:
    """The service adds no error handling of its own; the normalised exception
    travels up to the API layer."""
    ai, _ = service(error=error)

    with pytest.raises(type(error)):
        await ai.generate([a_message()])


async def test_structured_generation_delegates_with_the_schema() -> None:
    ai, provider = service()

    structured = await ai.generate_structured([a_message()], RefundSummary)

    assert provider.schemas == [RefundSummary]
    assert structured.data.pending == 3


async def test_structured_generation_also_validates_the_model() -> None:
    ai, provider = service()

    with pytest.raises(ValidationError):
        await ai.generate_structured([a_message()], RefundSummary, model="not-allowed")

    assert provider.requests == []


# -- Logging ------------------------------------------------------------------


async def test_the_prompt_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    ai, _ = service()

    with caplog.at_level(logging.DEBUG):
        await ai.generate([a_message()])

    assert PROMPT not in caplog.text
    assert COMPLETION not in caplog.text


async def test_the_tenant_is_recorded_for_attribution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    ai, _ = service()
    organization_id = uuid.uuid4()

    with caplog.at_level(logging.INFO, logger="app.services.ai"):
        await ai.generate([a_message()], organization_id=organization_id)

    record = next(r for r in caplog.records if r.message == "AI generation requested")
    assert record.context["organization_id"] == str(organization_id)  # type: ignore[attr-defined]
