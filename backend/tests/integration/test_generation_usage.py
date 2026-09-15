"""Direct generation, and the durable record it now leaves.

``/ai/generate`` calls the configured model and returns the answer. It creates
no agent run, no conversation and no tool execution - and that is preserved
here, asserted rather than assumed, because the whole reason this table exists
is that reusing the agent lifecycle would have meant changing what an agent run
is.

Two properties carry the file. **The record is attributed to the caller's own
tenant**, from the verified membership and from nothing a request body can
reach. And **the record is metadata**: model, tokens, latency, a correlation id,
and nothing that was said in either direction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.exceptions import LLMProviderError
from app.ai.gateway import LLMGateway, RetryPolicy
from app.ai.models import LLMRequest, LLMResponse, LLMUsage
from app.ai.providers.base import LLMProvider
from app.api.deps import get_llm_gateway
from app.models.agent_run import AgentRunRecord, AgentStepRecord
from app.models.conversation import Conversation
from app.models.generation import GenerationUsage
from tests.integration.auth_helpers import register

pytestmark = pytest.mark.integration

GENERATE = "/api/v1/ai/generate"

PROMPT = "What is the status of shipment ABC123 for Acme Freight?"
ANSWER = "It is in transit."


def a_body(text: str = PROMPT) -> dict[str, object]:
    return {"messages": [{"role": "user", "content": text}]}


async def rows(session: AsyncSession, organization_id: uuid.UUID) -> list[GenerationUsage]:
    result = await session.execute(
        select(GenerationUsage)
        .where(GenerationUsage.organization_id == organization_id)
        .order_by(GenerationUsage.created_at)
    )
    return list(result.scalars().all())


# -- A gateway, faked below the service ---------------------------------------
#
# ``get_llm_gateway`` is overridden rather than ``get_ai_service``, which is
# what the other /ai/generate tests replace. The difference is the point: this
# file is about what the *real* service does with a call's result, so the real
# service - and the real recorder it was built with - has to run.


class FakeProvider(LLMProvider):
    """Answers immediately with a fixed usage report."""

    name = "fake"

    model = "claude-opus-5"
    content = "It is in transit."
    input_tokens = 23
    output_tokens = 11

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=self.content,
            provider=self.name,
            model=request.model,
            usage=LLMUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens),
            latency_ms=7.0,
        )

    async def generate_structured(  # pragma: no cover - not reached by /ai/generate
        self, request: LLMRequest, schema: type
    ) -> object:
        raise AssertionError("Direct generation does not use structured output.")


class BrokenProvider(FakeProvider):
    """A provider that never answers."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMProviderError()


def _install(app: FastAPI, provider: LLMProvider) -> FakeProvider:
    def override() -> LLMGateway:
        return LLMGateway(
            provider,
            retry=RetryPolicy(max_retries=0),
            instruments=getattr(app.state, "instruments", None),
        )

    app.dependency_overrides[get_llm_gateway] = override
    return provider  # type: ignore[return-value]


@pytest.fixture
def fake_gateway(app: FastAPI) -> Iterator[FakeProvider]:
    provider = _install(app, FakeProvider())
    yield provider
    app.dependency_overrides.pop(get_llm_gateway, None)


@pytest.fixture
def failing_gateway(app: FastAPI) -> Iterator[FakeProvider]:
    provider = _install(app, BrokenProvider())
    yield provider
    app.dependency_overrides.pop(get_llm_gateway, None)


# -- The record ---------------------------------------------------------------


async def test_a_direct_generation_is_recorded(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code == 200
    assert len(await rows(session, account.organization_id)) == 1


async def test_the_record_carries_the_model_the_tokens_and_the_latency(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    account = await register(api_client)
    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    [usage] = await rows(session, account.organization_id)

    assert usage.model == fake_gateway.model
    assert usage.input_tokens == fake_gateway.input_tokens
    assert usage.output_tokens == fake_gateway.output_tokens
    assert usage.latency_ms >= 0


async def test_the_tenant_comes_from_the_verified_membership(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    """Not from the body: the request schema has no field that could name one,
    and the recorder is built from the membership the endpoint authorised."""
    account = await register(api_client)

    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    [usage] = await rows(session, account.organization_id)
    assert usage.organization_id == account.organization_id
    assert usage.user_id == account.user_id


async def test_a_body_that_names_another_organization_is_refused(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    """Structurally: the schema forbids unknown fields, so this never reaches
    a place where it could be believed."""
    account = await register(api_client)
    intruder = uuid.uuid4()

    response = await api_client.post(
        GENERATE,
        json={**a_body(), "organization_id": str(intruder)},
        headers=account.headers(),
    )

    assert response.status_code == 422
    assert await rows(session, account.organization_id) == []


async def test_the_request_id_is_correlation_and_nothing_else(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    """It ties a figure back to a request. It is not a key: two calls carrying
    the same one are two calls, and nothing deduplicates on it."""
    account = await register(api_client)
    supplied = "a-correlation-id-from-upstream"

    await api_client.post(
        GENERATE,
        json=a_body(),
        headers={**account.headers(), "X-Request-ID": supplied},
    )
    await api_client.post(
        GENERATE,
        json=a_body(),
        headers={**account.headers(), "X-Request-ID": supplied},
    )

    recorded = await rows(session, account.organization_id)
    assert len(recorded) == 2, "a correlation id does not deduplicate anything"
    assert {usage.request_id for usage in recorded} == {supplied}


# -- What it does not record --------------------------------------------------


async def test_no_prompt_or_response_is_persisted(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    """The rule the rest of the operational tables follow."""
    account = await register(api_client)
    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    [usage] = await rows(session, account.organization_id)
    written = " ".join(str(getattr(usage, column.name)) for column in GenerationUsage.__table__.c)

    assert PROMPT not in written
    assert "ABC123" not in written
    assert "Acme" not in written
    assert fake_gateway.content not in written


async def test_the_table_has_no_column_for_content_a_provider_or_a_key() -> None:
    """Structural, and the cheapest guarantee in the file: there is nowhere to
    put any of these, so no future change can put them there by accident."""
    columns = {column.name for column in GenerationUsage.__table__.c}

    assert columns == {
        "id",
        "organization_id",
        "user_id",
        "request_id",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "created_at",
    }
    for forbidden in (
        "provider",
        "prompt",
        "response",
        "content",
        "messages",
        "arguments",
        "result",
        "idempotency_key",
        "api_key",
    ):
        assert forbidden not in columns


# -- What it does not disturb -------------------------------------------------


async def test_a_direct_generation_creates_no_agent_run_or_conversation(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    """The constraint the whole design turns on: the Part 16 lifecycle is not
    borrowed to make a Part 19 figure add up."""
    account = await register(api_client)
    await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    runs = (
        (
            await session.execute(
                select(AgentRunRecord).where(
                    AgentRunRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    steps = (
        (
            await session.execute(
                select(AgentStepRecord).where(
                    AgentStepRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    conversations = (
        (
            await session.execute(
                select(Conversation).where(Conversation.organization_id == account.organization_id)
            )
        )
        .scalars()
        .all()
    )

    assert runs == []
    assert steps == []
    assert conversations == []


async def test_a_failed_generation_records_nothing(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, failing_gateway: FakeProvider
) -> None:
    """A call that never returned spent no tokens anybody can account for.
    The gateway counts the failure; the ledger stays empty."""
    account = await register(api_client)

    response = await api_client.post(GENERATE, json=a_body(), headers=account.headers())

    assert response.status_code >= 500
    assert await rows(session, account.organization_id) == []


# -- Tenancy ------------------------------------------------------------------


async def test_one_organization_does_not_see_anothers_generation_usage(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession, fake_gateway: FakeProvider
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)

    await api_client.post(GENERATE, json=a_body(), headers=theirs.headers())

    assert await rows(session, ours.organization_id) == []
    assert len(await rows(session, theirs.organization_id)) == 1
