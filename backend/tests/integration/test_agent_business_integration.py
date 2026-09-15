"""The whole chain, end to end.

    Agent -> ToolRegistry -> ToolExecutor -> BusinessTool -> Repository -> Database

Only the model is scripted. Everything below the decision is the real thing:
the real registry, the real executor, the real tools, the real repositories and
a real PostgreSQL row. No provider is contacted and no credential is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.decisions import AgentDecisionEnvelope, FinalDecision, ToolRequestDecision
from app.agents.models import AgentRun, AgentRunStatus
from app.agents.registry import DEMO_AGENT_ID, AgentRegistry, build_demo_agent
from app.agents.runtime import AgentRuntime
from app.ai.gateway import LLMGateway, RetryPolicy
from app.ai.models import LLMMessage, LLMRequest, LLMResponse, LLMStructuredResponse, LLMUsage
from app.ai.providers.base import LLMProvider
from app.core.config import Settings
from app.demo.seed import seed_demo_data
from app.tools.business import build_business_registry
from app.tools.executor import ToolExecutor
from app.tools.models import ToolOutcome

pytestmark = pytest.mark.integration

NORTHWIND = "demo-northwind"
GLOBEX = "demo-globex"


class ScriptedProvider(LLMProvider):
    """Stands in for the model. Decides, and nothing more."""

    name = "scripted"

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = decisions
        self.calls = 0
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise AssertionError("The runtime uses structured generation.")

    async def generate_structured[DataT: BaseModel](
        self, request: LLMRequest, schema: type[DataT]
    ) -> LLMStructuredResponse[DataT]:
        self.calls += 1
        self.requests.append(request)
        decision = self.decisions[min(self.calls - 1, len(self.decisions) - 1)]
        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=AgentDecisionEnvelope(decision=decision),
            response=LLMResponse(
                content="",
                provider=self.name,
                model=request.model,
                usage=LLMUsage(input_tokens=20, output_tokens=8),
                latency_ms=9.0,
            ),
        )


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def demo(session: AsyncSession) -> AsyncIterator[dict[str, uuid.UUID]]:
    yield await seed_demo_data(session)


ANSWERED = FinalDecision(content="Here is what I found.")


def runtime_for(
    session: AsyncSession, *decisions: Any, max_steps: int = 6
) -> tuple[AgentRuntime, ScriptedProvider]:
    """A runtime whose model plays the given decisions in order.

    A final answer is appended unless the caller supplied one, because the loop
    now continues after a tool and needs somewhere to stop.
    """
    script = list(decisions)
    if not isinstance(script[-1], FinalDecision):
        script.append(ANSWERED)

    provider = ScriptedProvider(script)
    settings = Settings(app_env="test", agent_max_steps=max_steps)
    gateway = LLMGateway(provider, retry=RetryPolicy(max_retries=0), sleep=no_sleep)

    executor = ToolExecutor(build_business_registry(session), settings)
    agents = AgentRegistry([build_demo_agent(settings)])

    return AgentRuntime(gateway, agents, settings, executor), provider


async def ask(runtime: AgentRuntime, organization_id: uuid.UUID, question: str) -> AgentRun:
    return await runtime.run(
        DEMO_AGENT_ID,
        [LLMMessage.user(question)],
        organization_id=organization_id,
        user_id=uuid.uuid4(),
        request_id="req-integration",
    )


# -- The demo scenario --------------------------------------------------------


async def test_the_agent_reaches_a_real_shipment_through_the_framework(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The headline: a model decision becomes a database row."""
    runtime, provider = runtime_for(
        session,
        ToolRequestDecision(tool_name="get_shipment", arguments={"shipment_reference": "ABC123"}),
    )

    run = await ask(runtime, demo[NORTHWIND], "Where is shipment ABC123?")

    assert provider.calls == 2, "look it up, then answer"
    assert run.status is AgentRunStatus.COMPLETED
    assert run.tool_calls
    assert run.tool_calls[0].result.outcome is ToolOutcome.SUCCEEDED
    assert run.tool_calls[0].result.data["shipment"]["status"] == "in_transit"  # type: ignore[index]
    assert run.tool_calls[0].result.data["shipment"]["destination"] == "Hamburg, DE"  # type: ignore[index]


async def test_the_agent_reaches_real_charges_and_a_computed_total(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The second half of the demo question: what is still owed.

    The total is the application's arithmetic, not the model's.
    """
    runtime, _ = runtime_for(
        session,
        ToolRequestDecision(
            tool_name="get_shipment_charges", arguments={"shipment_reference": "ABC123"}
        ),
    )

    run = await ask(runtime, demo[NORTHWIND], "Are there outstanding charges on ABC123?")

    assert run.tool_calls and run.tool_calls[0].result.ok
    total = Decimal(str(run.tool_calls[0].result.data["total_outstanding"]))  # type: ignore[index]
    assert total == Decimal("1430.50")
    assert run.tool_calls[0].result.data["currency"] == "GBP"  # type: ignore[index]


async def test_the_execution_is_correlated_with_the_run(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """request_id -> run_id -> tool_execution_id, unbroken."""
    runtime, _ = runtime_for(
        session,
        ToolRequestDecision(tool_name="get_shipment", arguments={"shipment_reference": "ABC123"}),
    )

    run = await ask(runtime, demo[NORTHWIND], "Where is ABC123?")

    assert run.request_id == "req-integration"
    assert run.tool_calls
    assert isinstance(run.tool_calls[0].result.tool_execution_id, uuid.UUID)


# -- Tenancy, all the way down ------------------------------------------------


async def test_an_agent_cannot_reach_another_tenants_shipment(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Globex's XYZ999 exists. A Northwind run must not find it."""
    runtime, _ = runtime_for(
        session,
        ToolRequestDecision(tool_name="get_shipment", arguments={"shipment_reference": "XYZ999"}),
    )

    run = await ask(runtime, demo[NORTHWIND], "Where is XYZ999?")

    assert run.tool_calls
    assert run.tool_calls[0].result.outcome is ToolOutcome.FAILED
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "shipment_not_found"
    assert str(demo[GLOBEX]) not in str(run.model_dump())


async def test_the_same_reference_answers_differently_per_tenant(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    decision = ToolRequestDecision(
        tool_name="get_shipment", arguments={"shipment_reference": "ABC123"}
    )

    northwind_runtime, _ = runtime_for(session, decision)
    globex_runtime, _ = runtime_for(session, decision)

    ours = await ask(northwind_runtime, demo[NORTHWIND], "Where is ABC123?")
    theirs = await ask(globex_runtime, demo[GLOBEX], "Where is ABC123?")

    assert ours.tool_calls and theirs.tool_calls
    assert ours.tool_calls[0].result.data["shipment"]["destination"] == "Hamburg, DE"  # type: ignore[index]
    assert theirs.tool_calls[0].result.data["shipment"]["destination"] == "Denver, US"  # type: ignore[index]


async def test_a_model_supplied_tenant_is_refused_before_any_query(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The model may name a tool and its arguments. It may not name a tenant."""
    runtime, _ = runtime_for(
        session,
        ToolRequestDecision(
            tool_name="get_shipment",
            arguments={"shipment_reference": "ABC123", "organization_id": str(demo[GLOBEX])},
        ),
    )

    run = await ask(runtime, demo[NORTHWIND], "Where is ABC123?")

    assert run.tool_calls
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_invalid_arguments"


# -- The rest of the runtime is unchanged -------------------------------------


async def test_a_final_answer_still_touches_no_tool(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    runtime, _ = runtime_for(session, FinalDecision(content="Nothing to look up."))

    run = await ask(runtime, demo[NORTHWIND], "Hello.")

    assert run.final_response == "Nothing to look up."
    assert run.tool_calls == []


async def test_the_full_business_workflow_runs_end_to_end(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The Part 15 acceptance scenario, against real rows.

    Two tools, three model calls, one answer - and the outstanding figure the
    model is given came from the application's arithmetic, not its own.
    """
    runtime, provider = runtime_for(
        session,
        ToolRequestDecision(tool_name="get_shipment", arguments={"shipment_reference": "ABC123"}),
        ToolRequestDecision(
            tool_name="get_shipment_charges", arguments={"shipment_reference": "ABC123"}
        ),
        FinalDecision(content="ABC123 is in transit; GBP 1430.50 is outstanding."),
    )

    run = await ask(
        runtime,
        demo[NORTHWIND],
        "Check shipment ABC123 and tell me if there are outstanding charges.",
    )

    assert provider.calls == 3
    assert run.step_count == 3
    assert [call.tool_name for call in run.tool_calls] == [
        "get_shipment",
        "get_shipment_charges",
    ]
    assert run.final_response == "ABC123 is in transit; GBP 1430.50 is outstanding."

    # Both results were handed back for the model to reason over.
    shown = provider.requests[-1].messages
    tool_turns = [m for m in shown if m.role.value == "tool"]
    assert len(tool_turns) == 2
    assert tool_turns[0].tool is not None and tool_turns[1].tool is not None
    assert tool_turns[0].tool.data["shipment"]["status"] == "in_transit"
    assert Decimal(str(tool_turns[1].tool.data["total_outstanding"])) == Decimal("1430.50")


async def test_an_unknown_tool_is_still_a_controlled_result(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    runtime, _ = runtime_for(session, ToolRequestDecision(tool_name="delete_everything"))

    run = await ask(runtime, demo[NORTHWIND], "Delete it all.")

    assert run.status is AgentRunStatus.COMPLETED
    assert run.tool_calls
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_not_found"
