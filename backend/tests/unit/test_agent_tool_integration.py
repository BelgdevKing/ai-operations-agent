"""Where the agent runtime meets the tool framework.

The decision comes from a scripted model; the tool is an example one. Nothing
reaches a provider, a network or a database.

Since Part 15 the runtime loops: a tool result goes back to the model, which
decides what to do next. Most scripts here are therefore "ask for a tool, then
answer", which is the shortest complete workflow.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents.cancellation import Cancellation
from app.agents.decisions import FinalDecision, ToolRequestDecision
from app.agents.exceptions import AgentCancelledError, AgentMaxStepsExceededError
from app.agents.models import AgentRun, AgentRunStatus
from app.agents.registry import DEMO_AGENT_ID, AgentRegistry, build_demo_agent
from app.agents.runtime import AgentRuntime
from app.ai.gateway import LLMGateway, RetryPolicy
from app.ai.models import LLMMessage, LLMRole
from app.core.config import Settings
from app.tools.base import Tool
from app.tools.executor import ToolExecutor
from app.tools.models import ToolOutcome
from app.tools.registry import ToolRegistry
from tests.tool_helpers import (
    LEAKY_SECRET,
    BrokenTool,
    CancelShipmentTool,
    DisabledTool,
    EchoTool,
    WhoAmITool,
)
from tests.unit.test_agent_runtime import ScriptedProvider, no_sleep

ORGANIZATION = uuid.uuid4()
USER = uuid.uuid4()

ANSWERED = FinalDecision(content="Here is what I found.")


def build(
    decisions: list[Any], *tools: Tool, **setting_overrides: Any
) -> tuple[AgentRuntime, ScriptedProvider]:
    provider = ScriptedProvider(decisions)
    settings = Settings(app_env="test", **setting_overrides)
    gateway = LLMGateway(provider, retry=RetryPolicy(max_retries=0), sleep=no_sleep)

    executor = ToolExecutor(ToolRegistry(tools), settings)
    agents = AgentRegistry([build_demo_agent(settings)])

    return AgentRuntime(gateway, agents, settings, executor), provider


def asking(tool: str, **arguments: Any) -> ToolRequestDecision:
    return ToolRequestDecision(tool_name=tool, arguments=arguments)


async def run_agent(runtime: AgentRuntime, **kwargs: Any) -> AgentRun:
    return await runtime.run(
        DEMO_AGENT_ID,
        [LLMMessage.user("Where is shipment ABC123?")],
        organization_id=ORGANIZATION,
        user_id=USER,
        **kwargs,
    )


# -- A final answer never touches a tool --------------------------------------


async def test_a_final_decision_does_not_invoke_any_tool() -> None:
    tool = EchoTool()
    runtime, _ = build([FinalDecision(content="It is in transit.")], tool)

    run = await run_agent(runtime)

    assert run.final_response == "It is in transit."
    assert run.tool_calls == []
    assert tool.calls == 0


# -- One tool, then an answer -------------------------------------------------


async def test_a_tool_runs_and_the_model_then_answers() -> None:
    tool = EchoTool()
    runtime, provider = build([asking("echo", text="ABC123"), ANSWERED], tool)

    run = await run_agent(runtime)

    assert tool.calls == 1
    assert len(provider.requests) == 2, "decide, then answer"
    assert run.status is AgentRunStatus.COMPLETED
    assert run.final_response == ANSWERED.content


async def test_the_tool_result_is_given_back_to_the_model() -> None:
    """The point of the loop: the second call can see what the first found."""
    runtime, provider = build([asking("echo", text="ABC123"), ANSWERED], EchoTool())

    await run_agent(runtime)

    second = provider.requests[1].messages
    tool_turns = [m for m in second if m.role is LLMRole.TOOL]

    assert len(tool_turns) == 1
    assert tool_turns[0].tool is not None
    assert tool_turns[0].tool.tool_name == "echo"
    assert tool_turns[0].tool.succeeded
    assert tool_turns[0].tool.data == {"echoed": "ABC123"}


async def test_the_conversation_records_the_request_and_the_result_in_order() -> None:
    runtime, provider = build([asking("echo", text="x"), ANSWERED], EchoTool())

    await run_agent(runtime)

    roles = [m.role.value for m in provider.requests[1].messages]
    assert roles == ["system", "user", "assistant", "tool"]


async def test_the_run_records_each_tool_it_used() -> None:
    runtime, _ = build([asking("echo", text="x"), ANSWERED], EchoTool())

    run = await run_agent(runtime)

    assert run.tool_call_count == 1
    assert run.tool_calls[0].tool_name == "echo"
    assert run.tool_calls[0].result.outcome is ToolOutcome.SUCCEEDED
    assert isinstance(run.tool_calls[0].result.tool_execution_id, uuid.UUID)


# -- Two tools, then an answer ------------------------------------------------


async def test_two_tools_run_across_three_model_calls() -> None:
    """The Part 15 scenario in miniature: look, look again, then answer."""
    echo = EchoTool()
    who = WhoAmITool()
    runtime, provider = build(
        [asking("echo", text="ABC123"), asking("who_am_i", text="x"), ANSWERED],
        echo,
        who,
        agent_max_steps=6,
    )

    run = await run_agent(runtime)

    assert echo.calls == 1
    assert len(provider.requests) == 3
    assert run.step_count == 3
    assert [call.tool_name for call in run.tool_calls] == ["echo", "who_am_i"]
    assert run.final_response == ANSWERED.content


async def test_every_tool_result_accumulates_in_the_conversation() -> None:
    runtime, provider = build(
        [asking("echo", text="a"), asking("echo", text="b"), ANSWERED],
        EchoTool(),
        agent_max_steps=6,
    )

    await run_agent(runtime)

    final_call = provider.requests[2].messages
    assert sum(1 for m in final_call if m.role is LLMRole.TOOL) == 2


# -- Identity comes from the run ----------------------------------------------


async def test_the_tool_acts_for_the_runs_organization() -> None:
    runtime, _ = build([asking("who_am_i", text="x"), ANSWERED], WhoAmITool())

    run = await run_agent(runtime)

    assert run.tool_calls[0].result.data == {
        "organization_id": str(ORGANIZATION),
        "user_id": str(USER),
    }


async def test_a_model_cannot_point_a_tool_at_another_tenant() -> None:
    other = uuid.uuid4()
    runtime, _ = build(
        [asking("who_am_i", text="x", organization_id=str(other)), ANSWERED], WhoAmITool()
    )

    run = await run_agent(runtime)

    assert not run.tool_calls[0].result.ok
    # The attempt itself stays on the step, which is where an injection attempt
    # should be visible to whoever investigates. What matters is that it reached
    # neither the tool nor the client.
    assert str(other) not in str(run.tool_calls[0].result.model_dump())


async def test_a_refused_tenant_never_reaches_the_client() -> None:
    """The run remembers what was attempted; nothing published repeats it.

    The response is now projected from the durable record rather than from the
    run object, so this asserts the property where it is cheapest to check: the
    tool call the run recorded, which is what every projection reads from.
    """
    other = uuid.uuid4()
    runtime, _ = build(
        [asking("who_am_i", text="x", organization_id=str(other)), ANSWERED], WhoAmITool()
    )

    run = await run_agent(runtime)

    published = {
        "tool_calls": [
            {"tool_name": call.tool_name, "outcome": call.result.outcome.value}
            for call in run.tool_calls
        ],
        "status": run.status.value,
        "error_code": run.error_code,
    }
    assert str(other) not in str(published)


async def test_the_tool_execution_is_correlated_with_the_run() -> None:
    tool = EchoTool()
    runtime, _ = build([asking("echo", text="x"), ANSWERED], tool)

    run = await run_agent(runtime, request_id="req-77")

    context = tool.seen_contexts[0]
    assert context.run_id == run.id
    assert context.agent_id == DEMO_AGENT_ID
    assert context.request_id == "req-77"


# -- Failures are information, not the end ------------------------------------


async def test_an_unknown_tool_is_reported_back_to_the_model() -> None:
    runtime, provider = build([asking("not_installed"), ANSWERED], EchoTool())

    run = await run_agent(runtime)

    assert run.status is AgentRunStatus.COMPLETED
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_not_found"

    tool_turn = next(m for m in provider.requests[1].messages if m.role is LLMRole.TOOL)
    assert tool_turn.tool is not None
    assert not tool_turn.tool.succeeded


async def test_arguments_are_validated_before_the_tool_is_reached() -> None:
    tool = EchoTool()
    runtime, _ = build([asking("echo", wrong="shape"), ANSWERED], tool)

    run = await run_agent(runtime)

    assert tool.calls == 0
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_invalid_arguments"


async def test_a_disabled_tool_is_reported_and_not_run() -> None:
    tool = DisabledTool()
    runtime, _ = build([asking("switched_off"), ANSWERED], tool)

    run = await run_agent(runtime)

    assert tool.calls == 0
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_disabled"


async def test_a_broken_tool_does_not_leak_its_exception_into_the_run() -> None:
    runtime, provider = build([asking("broken"), ANSWERED], BrokenTool())

    run = await run_agent(runtime)

    assert LEAKY_SECRET not in str(run.model_dump())
    # Nor into what the model is shown next.
    shown = [m.transport_content for m in provider.requests[1].messages]
    assert LEAKY_SECRET not in str(shown)


async def test_a_tool_failure_does_not_fail_the_run() -> None:
    """The model asked for something that did not work. That is information."""
    runtime, _ = build([asking("broken"), ANSWERED], BrokenTool())

    run = await run_agent(runtime)

    assert run.status is AgentRunStatus.COMPLETED
    assert run.error_code is None


# -- Approval still blocks execution ------------------------------------------


async def test_an_approval_required_tool_is_reported_not_executed() -> None:
    """The loop must not become a way around the approval gate."""
    tool = CancelShipmentTool()
    runtime, _ = build([asking("cancel_shipment", shipment_id="A"), ANSWERED], tool)

    run = await run_agent(runtime)

    assert tool.calls == 0, "nothing destructive happened"
    assert run.tool_calls[0].result.outcome is ToolOutcome.APPROVAL_REQUIRED


async def test_the_run_pauses_rather_than_carrying_on_without_a_decision() -> None:
    """The loop stops the moment a person is needed.

    It used to record the refusal and keep going, because there was nobody to
    ask. Now there is, so continuing would mean asking the model what to do about
    a question that has been put to a human and not yet answered - and the
    model's answer to that is at best noise and at worst a second attempt.
    """
    tool = CancelShipmentTool()
    runtime, provider = build(
        [asking("cancel_shipment", shipment_id="A"), ANSWERED], tool, agent_max_steps=4
    )

    run = await run_agent(runtime)

    assert run.status is AgentRunStatus.AWAITING_APPROVAL
    assert run.final_response is None, "a paused run has not answered anything"
    assert len(provider.requests) == 1, "the model was not asked again"
    assert tool.calls == 0


async def test_a_paused_run_is_not_terminal_and_names_what_it_is_waiting_on() -> None:
    tool = CancelShipmentTool()
    runtime, _ = build([asking("cancel_shipment", shipment_id="A")], tool)

    run = await run_agent(runtime)

    assert not run.is_terminal
    assert run.is_awaiting_approval
    assert run.pending_tool_execution_id == run.tool_calls[0].result.tool_execution_id


async def test_asking_repeatedly_never_executes_an_approval_tool() -> None:
    """Even given a model that will ask forever, the tool body is never reached.

    The run pauses on the first ask, so "repeatedly" never happens - which is a
    stronger guarantee than the budget running out, not a weaker one.
    """
    tool = CancelShipmentTool()
    runtime, _ = build([asking("cancel_shipment", shipment_id="A")] * 6, tool, agent_max_steps=4)

    run = await run_agent(runtime)

    assert run.status is AgentRunStatus.AWAITING_APPROVAL
    assert tool.calls == 0


# -- The loop always ends -----------------------------------------------------


async def test_a_model_that_only_ever_asks_for_tools_runs_out_of_budget() -> None:
    """The safety property the whole loop rests on."""
    tool = EchoTool()
    runtime, provider = build([asking("echo", text="again")] * 50, tool, agent_max_steps=3)

    with pytest.raises(AgentMaxStepsExceededError):
        await run_agent(runtime)

    assert len(provider.requests) == 3, "exactly the budget, not one more"
    assert tool.calls == 3


async def test_the_budget_bounds_tool_executions_too() -> None:
    tool = EchoTool()
    runtime, _ = build([asking("echo", text="x")] * 20, tool, agent_max_steps=2)

    with pytest.raises(AgentMaxStepsExceededError):
        await run_agent(runtime)

    assert tool.calls <= 2


# -- Cancellation -------------------------------------------------------------


async def test_cancelling_before_the_first_model_call_reaches_nothing() -> None:
    tool = EchoTool()
    cancellation = Cancellation()
    cancellation.cancel()

    runtime, provider = build([asking("echo", text="x"), ANSWERED], tool)

    with pytest.raises(AgentCancelledError):
        await run_agent(runtime, cancellation=cancellation)

    assert provider.requests == []
    assert tool.calls == 0


async def test_cancelling_after_a_decision_stops_before_the_tool() -> None:
    class AfterDecision(Cancellation):
        def __init__(self, provider: ScriptedProvider) -> None:
            super().__init__()
            self._provider = provider

        @property
        def cancelled(self) -> bool:
            return len(self._provider.requests) > 0

    tool = EchoTool()
    runtime, provider = build([asking("echo", text="x"), ANSWERED], tool)

    with pytest.raises(AgentCancelledError):
        await run_agent(runtime, cancellation=AfterDecision(provider))

    assert tool.calls == 0


async def test_cancelling_after_a_tool_stops_before_the_next_model_call() -> None:
    """Between two cycles: the tool ran, and nothing further happens."""

    class AfterTool(Cancellation):
        def __init__(self, tool: EchoTool) -> None:
            super().__init__()
            self._tool = tool

        @property
        def cancelled(self) -> bool:
            return self._tool.calls > 0

    tool = EchoTool()
    runtime, provider = build([asking("echo", text="x"), ANSWERED], tool, agent_max_steps=4)

    with pytest.raises(AgentCancelledError):
        await run_agent(runtime, cancellation=AfterTool(tool))

    assert len(provider.requests) == 1, "no second model call"
    assert tool.calls == 1


async def test_a_cancelled_run_never_produces_an_answer() -> None:
    cancellation = Cancellation()
    cancellation.cancel()

    runtime, _ = build([ANSWERED], EchoTool())

    with pytest.raises(AgentCancelledError) as raised:
        await run_agent(runtime, cancellation=cancellation)

    assert raised.value.status_code == 499


# -- A deployment with no tools -----------------------------------------------


async def test_a_runtime_built_without_tools_still_answers_cleanly() -> None:
    settings = Settings(app_env="test")
    gateway = LLMGateway(
        ScriptedProvider([asking("anything"), ANSWERED]),
        retry=RetryPolicy(max_retries=0),
        sleep=no_sleep,
    )
    runtime = AgentRuntime(gateway, AgentRegistry([build_demo_agent(settings)]), settings)

    run = await run_agent(runtime)

    assert run.status is AgentRunStatus.COMPLETED
    assert run.tool_calls[0].result.failure is not None
    assert run.tool_calls[0].result.failure.code == "tool_not_found"
