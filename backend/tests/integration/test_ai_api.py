"""POST /api/v1/ai/generate, end to end through authentication and tenancy.

Uses the existing PostgreSQL integration fixtures for authentication, and
overrides the AI service dependency so no provider is ever called. Skipped when
PostgreSQL is unreachable, like every other integration test.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.models import LLMResponse, LLMUsage
from app.api.deps import get_ai_service
from app.models import Organization, OrganizationMember, User
from app.models.enums import MemberRole, MembershipStatus, OrganizationStatus, UserStatus
from app.schemas.ai import MAX_MESSAGE_CHARACTERS, MAX_MESSAGES, MAX_OUTPUT_TOKENS_LIMIT
from tests.integration.auth_helpers import build_organization, register

pytestmark = pytest.mark.integration

GENERATE = "/api/v1/ai/generate"
PROMPT = "Explain what an AI operations agent is."
COMPLETION = "An AI operations agent carries out business tasks under supervision."
FAKE_KEY = "test-anthropic-secret"


class StubAIService:
    """Stands in for AIService, so no gateway or provider is involved."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.last: dict[str, Any] = {}

    async def generate(self, messages: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.last = {"messages": list(messages), **kwargs}
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=COMPLETION,
            provider="stub-provider",
            model="stub-model",
            usage=LLMUsage(input_tokens=42, output_tokens=17),
            latency_ms=12.5,
            request_id="req_provider_secret",
        )


@pytest.fixture
def ai_service(app: FastAPI) -> Iterator[StubAIService]:
    """Replace the AI service for the application under test."""
    stub = StubAIService()
    app.dependency_overrides[get_ai_service] = lambda: stub
    yield stub
    app.dependency_overrides.pop(get_ai_service, None)


def fail_with(app: FastAPI, error: Exception) -> StubAIService:
    stub = StubAIService(error=error)
    app.dependency_overrides[get_ai_service] = lambda: stub
    return stub


def a_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": PROMPT}]}
    body.update(overrides)
    return body


# -- Authentication -----------------------------------------------------------


async def test_an_unauthenticated_request_is_refused(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    response = await api_client.post(GENERATE, json=a_body())

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert ai_service.calls == 0


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
async def test_a_malformed_token_is_refused(
    header: dict[str, str], api_client: AsyncClient, ai_service: StubAIService
) -> None:
    response = await api_client.post(GENERATE, json=a_body(), headers=header)

    assert response.status_code == 401
    assert ai_service.calls == 0


async def test_an_authenticated_member_may_call(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 200
    assert ai_service.calls == 1


# -- Tenancy ------------------------------------------------------------------


async def test_the_organization_comes_from_the_verified_membership(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    """Not from anything in the request - there is no field for it."""
    account = await register(api_client)

    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert ai_service.last["organization_id"] == account.organization_id


async def test_an_organization_cannot_be_named_in_the_body(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    """A forbidden extra field, so the attempt is a 422 rather than ignored."""
    org_a = await build_organization(api_client, session)
    org_b = await build_organization(api_client, session)

    response = await api_client.post(
        GENERATE,
        json=a_body(organization_id=str(org_b.id)),
        headers=org_a.owner.headers(),
    )

    assert response.status_code == 422
    assert ai_service.calls == 0


async def test_a_foreign_organization_header_is_refused(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    org_a = await build_organization(api_client, session)
    org_b = await build_organization(api_client, session)

    response = await api_client.post(GENERATE, json=a_body(), headers=org_a.owner.headers(org_b.id))

    assert response.status_code == 403
    assert ai_service.calls == 0


async def test_a_suspended_user_cannot_call(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    account = await register(api_client)
    user = await session.get(User, account.user_id)
    assert user is not None
    user.status = UserStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 401
    assert ai_service.calls == 0


async def test_a_suspended_membership_cannot_call(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    from sqlalchemy import select

    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]
    row = (
        await session.execute(
            select(OrganizationMember)
            .where(OrganizationMember.user_id == member.user_id)
            .where(OrganizationMember.organization_id == org.id)
        )
    ).scalar_one()
    row.status = MembershipStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=member.headers())

    assert response.status_code == 403
    assert ai_service.calls == 0


async def test_a_suspended_organization_cannot_call(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    account = await register(api_client)
    organization = await session.get(Organization, account.organization_id)
    assert organization is not None
    organization.status = OrganizationStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 403
    assert ai_service.calls == 0


# -- Request validation -------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user"}]},
        {},
    ],
    ids=["empty", "blank content", "invalid role", "missing content", "no messages key"],
)
async def test_invalid_conversations_are_rejected(
    body: dict[str, Any], api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=body, headers=account.headers())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert ai_service.calls == 0


async def test_too_many_messages_are_rejected(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)
    body = {"messages": [{"role": "user", "content": "hi"}] * (MAX_MESSAGES + 1)}

    response = await api_client.post(GENERATE, json=body, headers=account.headers())

    assert response.status_code == 422
    assert ai_service.calls == 0


async def test_an_over_long_message_is_rejected(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)
    body = a_body(messages=[{"role": "user", "content": "x" * (MAX_MESSAGE_CHARACTERS + 1)}])

    response = await api_client.post(GENERATE, json=body, headers=account.headers())

    assert response.status_code == 422
    assert ai_service.calls == 0


async def test_an_oversized_conversation_is_rejected(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    """Each message within its limit, but the whole conversation beyond it."""
    account = await register(api_client)
    body = {
        "messages": [{"role": "user", "content": "x" * MAX_MESSAGE_CHARACTERS}] * 10,
    }

    response = await api_client.post(GENERATE, json=body, headers=account.headers())

    assert response.status_code == 422
    assert ai_service.calls == 0


@pytest.mark.parametrize("temperature", [-0.1, 2.1, 100])
async def test_an_invalid_temperature_is_rejected(
    temperature: float, api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    response = await api_client.post(
        GENERATE, json=a_body(temperature=temperature), headers=account.headers()
    )

    assert response.status_code == 422
    assert ai_service.calls == 0


@pytest.mark.parametrize("tokens", [0, -1, MAX_OUTPUT_TOKENS_LIMIT + 1])
async def test_an_invalid_output_limit_is_rejected(
    tokens: int, api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    response = await api_client.post(
        GENERATE, json=a_body(max_output_tokens=tokens), headers=account.headers()
    )

    assert response.status_code == 422
    assert ai_service.calls == 0


async def test_a_provider_cannot_be_chosen_in_the_request(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    """The point of forbidding extra fields: an attempt to route around the
    server's configuration is refused, not quietly dropped."""
    account = await register(api_client)

    response = await api_client.post(
        GENERATE, json=a_body(provider="openai"), headers=account.headers()
    )

    assert response.status_code == 422
    assert ai_service.calls == 0


# -- Success ------------------------------------------------------------------


async def test_a_completion_is_returned(api_client: AsyncClient, ai_service: StubAIService) -> None:
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == COMPLETION
    assert body["model"] == "stub-model"


async def test_usage_is_preserved(api_client: AsyncClient, ai_service: StubAIService) -> None:
    account = await register(api_client)

    body = (await api_client.post(GENERATE, json=a_body(), headers=account.headers())).json()

    assert body["usage"] == {"input_tokens": 42, "output_tokens": 17, "total_tokens": 59}


async def test_the_service_is_called_exactly_once(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert ai_service.calls == 1


async def test_optional_parameters_reach_the_service(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    await api_client.post(
        GENERATE,
        json=a_body(temperature=0.4, max_output_tokens=128, model="some-model"),
        headers=account.headers(),
    )

    assert ai_service.last["temperature"] == 0.4
    assert ai_service.last["max_output_tokens"] == 128
    assert ai_service.last["model"] == "some-model"


async def test_a_request_needs_no_model(api_client: AsyncClient, ai_service: StubAIService) -> None:
    """The documented normal case."""
    account = await register(api_client)

    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert ai_service.last["model"] is None


async def test_the_response_exposes_only_the_intended_fields(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    body = (await api_client.post(GENERATE, json=a_body(), headers=account.headers())).json()

    assert set(body) == {"content", "model", "usage", "latency_ms"}


async def test_the_provider_is_not_disclosed(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    """Which vendor served the call is internal routing, not the contract."""
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert "stub-provider" not in response.text
    assert "provider" not in response.json()


async def test_the_provider_request_id_is_not_disclosed(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    """X-Request-ID already gives the caller a correlation handle, and it
    belongs to this application rather than a vendor."""
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert "req_provider_secret" not in response.text
    assert response.headers["X-Request-ID"]


# -- Error behaviour ----------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LLMAuthenticationError(provider="anthropic"), 500),
        (LLMConfigurationError(provider="anthropic"), 500),
        (LLMRateLimitError(provider="anthropic", retry_after_seconds=30), 429),
        (LLMTimeoutError(provider="anthropic"), 504),
        (LLMProviderError(provider="anthropic"), 502),
        (LLMInvalidResponseError(provider="anthropic"), 502),
    ],
)
async def test_normalized_failures_map_to_safe_responses(
    error: LLMError, status: int, api_client: AsyncClient, app: FastAPI
) -> None:
    fail_with(app, error)
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == error.code
    assert body["request_id"] == response.headers["X-Request-ID"]

    app.dependency_overrides.pop(get_ai_service, None)


async def test_a_provider_failure_never_names_the_provider(
    api_client: AsyncClient, app: FastAPI
) -> None:
    fail_with(app, LLMProviderError("upstream said: quota for key sk-live-123", provider="openai"))
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert "openai" not in response.text.lower()
    assert "sk-live-123" not in response.text
    app.dependency_overrides.pop(get_ai_service, None)


async def test_an_unexpected_failure_does_not_leak_internals(
    api_client: AsyncClient, app: FastAPI
) -> None:
    fail_with(app, RuntimeError("psycopg connection string postgres://user:pw@host/db"))
    account = await register(api_client)

    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 500
    assert "postgres://" not in response.text
    assert "RuntimeError" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
    app.dependency_overrides.pop(get_ai_service, None)


# -- Security -----------------------------------------------------------------


async def test_no_credential_or_prompt_reaches_the_logs(
    api_client: AsyncClient,
    ai_service: StubAIService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = await register(api_client)
    headers = account.headers()

    with caplog.at_level(logging.DEBUG):
        await api_client.post(GENERATE, json=a_body(), headers=headers)

    assert caplog.text
    assert FAKE_KEY not in caplog.text
    assert PROMPT not in caplog.text
    assert COMPLETION not in caplog.text
    # The bearer token is a credential too.
    assert headers["Authorization"].removeprefix("Bearer ") not in caplog.text
    assert "Authorization" not in caplog.text


async def test_no_sdk_object_is_serialized(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    for marker in ("anthropic", "openai", "Message(", "ChatCompletion", "_request_id"):
        assert marker not in response.text


async def test_a_configuration_failure_does_not_reveal_configuration(
    api_client: AsyncClient, app: FastAPI
) -> None:
    fail_with(app, LLMConfigurationError("ANTHROPIC_API_KEY is unset", provider="anthropic"))
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 500
    assert "ANTHROPIC_API_KEY" not in response.text
    app.dependency_overrides.pop(get_ai_service, None)


async def test_the_endpoint_is_not_reachable_without_a_membership(
    api_client: AsyncClient, session: AsyncSession, ai_service: StubAIService
) -> None:
    from sqlalchemy import delete

    account = await register(api_client)
    await session.execute(
        delete(OrganizationMember).where(OrganizationMember.user_id == account.user_id)
    )
    await session.flush()

    response = await api_client.post(
        GENERATE, json=a_body(), headers=account.headers(include_organization=False)
    )

    assert response.status_code == 403
    assert ai_service.calls == 0


def test_a_uuid_in_the_body_is_not_a_tenant_selector() -> None:
    """Structural: the request schema simply has no organization field."""
    from app.schemas.ai import GenerateRequest

    assert "organization_id" not in GenerateRequest.model_fields
    assert "provider" not in GenerateRequest.model_fields
    assert uuid.UUID  # the type is never used in this schema


# -- Dependency order: authenticate, authorize, then touch the provider --------
#
# These tests deliberately do NOT override get_ai_service. The test settings
# carry no provider credential, so the real gateway raises
# LLMConfigurationError the moment it is built. That makes the construction
# order observable: whichever runs first decides the status code.
#
# Before the fix, `service` was declared ahead of `membership`, so an anonymous
# request to a deployment with no credential answered 500 instead of 401.


async def test_an_unauthenticated_request_is_refused_before_the_provider_is_touched(
    api_client: AsyncClient,
) -> None:
    """401, even where the provider is misconfigured.

    Authentication is not conditional on the deployment being able to generate.
    """
    response = await api_client.post(GENERATE, json=a_body())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_a_suspended_organization_is_refused_before_the_provider_is_touched(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """403, decided by membership rather than by provider configuration."""
    account = await register(api_client)
    organization = await session.get(Organization, account.organization_id)
    assert organization is not None
    organization.status = OrganizationStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_a_suspended_membership_is_refused_before_the_provider_is_touched(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    from sqlalchemy import select

    account = await register(api_client)
    membership = (
        await session.execute(
            select(OrganizationMember).where(OrganizationMember.user_id == account.user_id)
        )
    ).scalar_one()
    membership.status = MembershipStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 403


async def test_a_suspended_user_is_refused_before_the_provider_is_touched(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """401: an inactive user's token stops working, whatever the deployment."""
    account = await register(api_client)
    user = await session.get(User, account.user_id)
    assert user is not None
    user.status = UserStatus.SUSPENDED
    await session.flush()

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 401


async def test_a_misconfigured_provider_surfaces_only_after_authorization(
    api_client: AsyncClient,
) -> None:
    """The configuration failure is reached, but only by an authorized caller.

    This is the other half of the ordering guarantee: authorization first does
    not mean the provider error disappears for someone entitled to generate.
    """
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "llm_configuration_error"


async def test_the_configuration_failure_names_no_environment_variable(
    api_client: AsyncClient,
) -> None:
    """A deployment problem must not describe the deployment to a client."""
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    body = response.text
    assert "ANTHROPIC_API_KEY" not in body
    assert "OPENAI_API_KEY" not in body
    assert "LLM_PROVIDER" not in body
    assert response.json()["error"]["details"] == {}


async def test_the_successful_path_is_unchanged_by_the_ordering_fix(
    api_client: AsyncClient, ai_service: StubAIService
) -> None:
    """With the service stubbed, an authorized caller still generates."""
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 200
    assert response.json()["content"] == COMPLETION
    assert ai_service.calls == 1
