"""Shared ground for the durable-execution tests.

A scripted provider behind the *real* gateway, the real registry, the real
runtime, the real loop and the real tool framework - so everything below the
vendor boundary actually executes. No network call and no credential is
involved, and the decisions the "model" makes are whatever the test wrote down.

Also here: the smallest business records a destructive tool needs something to
act on, created inside the test's own transaction and therefore rolled back with
it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.decisions import AgentDecisionEnvelope, FinalDecision, ToolRequestDecision
from app.agents.registry import DEMO_AGENT_ID
from app.ai.gateway import LLMGateway, RetryPolicy
from app.ai.models import LLMRequest, LLMResponse, LLMStructuredResponse, LLMUsage
from app.ai.providers.base import LLMProvider
from app.api.deps import get_llm_gateway
from app.models.business import Customer, Shipment
from app.models.enums import CustomerStatus, ShipmentStatus

RUN_URL = f"/api/v1/ai/agents/{DEMO_AGENT_ID}/run"

ANSWER = "The shipment has been dealt with."
REFUSED_ANSWER = "That was not approved, so nothing was cancelled."


class ScriptedProvider(LLMProvider):
    """Answers with the decisions a test wrote, in order.

    The last one repeats once the script runs out, so a test does not have to
    predict exactly how many calls a loop will make to say what it should do
    when it gets there.
    """

    name = "scripted"

    def __init__(self, *decisions: Any, error: Exception | None = None) -> None:
        self.decisions: list[Any] = list(decisions) or [FinalDecision(content=ANSWER)]
        self.error = error
        self.calls = 0
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise AssertionError("The runtime must use structured generation.")

    async def generate_structured[DataT: BaseModel](
        self, request: LLMRequest, schema: type[DataT]
    ) -> LLMStructuredResponse[DataT]:
        self.calls += 1
        self.requests.append(request)

        if self.error is not None:
            raise self.error

        decision = self.decisions[min(self.calls - 1, len(self.decisions) - 1)]
        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=AgentDecisionEnvelope(decision=decision),
            response=LLMResponse(
                content="",
                provider=self.name,
                model=request.model,
                usage=LLMUsage(input_tokens=11, output_tokens=7),
                latency_ms=12.0,
            ),
        )


async def _no_sleep(_seconds: float) -> None:
    return None


def script(app: FastAPI, *decisions: Any) -> ScriptedProvider:
    """Put a scripted provider behind the real gateway for this application.

    Returns the provider so a test can read what the model was actually sent -
    which is how the "a tool result never becomes an instruction" assertions are
    made about the bytes rather than about the intention.
    """
    scripted = ScriptedProvider(*decisions)

    def override() -> LLMGateway:
        return LLMGateway(scripted, retry=RetryPolicy(max_retries=0), sleep=_no_sleep)

    app.dependency_overrides[get_llm_gateway] = override
    return scripted


def asking(tool_name: str, **arguments: Any) -> ToolRequestDecision:
    return ToolRequestDecision(tool_name=tool_name, arguments=arguments)


def answering(content: str = ANSWER) -> FinalDecision:
    return FinalDecision(content=content)


def body(text: str = "Please cancel ABC123.", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": text}]}
    payload.update(extra)
    return payload


async def seed_shipment(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    reference: str = "ABC123",
    status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
) -> Shipment:
    """One customer and one shipment, in the organization given.

    Written directly rather than through the demo seed: these tests are about a
    run belonging to an account the test registered, and the demo dataset builds
    organizations of its own.
    """
    unique = uuid.uuid4().hex[:8]

    customer = Customer(
        organization_id=organization_id,
        reference=f"CUST-{unique}",
        name="A Customer",
        status=CustomerStatus.ACTIVE,
    )
    session.add(customer)
    await session.flush()

    shipment = Shipment(
        organization_id=organization_id,
        customer_id=customer.id,
        reference=reference,
        status=status,
        origin="Rotterdam",
        destination="Felixstowe",
    )
    session.add(shipment)
    await session.flush()

    return shipment
