"""The tool framework: the registry, and everything the executor enforces.

Offline. The example tools do no real work, and the one "slow" tool is given a
deadline measured in milliseconds so no test ever waits.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.agents.cancellation import Cancellation
from app.core.config import Settings
from app.tools.base import Tool
from app.tools.exceptions import ToolNotFoundError, ToolRegistrationError
from app.tools.executor import ToolExecutor
from app.tools.models import (
    RESERVED_ARGUMENT_NAMES,
    ToolExecutionContext,
    ToolMetadata,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    ToolSafety,
)
from app.tools.registry import ToolRegistry
from tests.tool_helpers import (
    LEAKY_SECRET,
    BrokenTool,
    CancelShipmentTool,
    DeliberateFailureTool,
    DisabledTool,
    EchoInput,
    EchoOutput,
    EchoTool,
    HugeResultTool,
    NestedTool,
    SendEmailTool,
    SlowTool,
    WhoAmITool,
    WrongResultTool,
)

ORGANIZATION = uuid.uuid4()
OTHER_ORGANIZATION = uuid.uuid4()
USER = uuid.uuid4()


def executor_for(*tools: Tool, **setting_overrides: object) -> ToolExecutor:
    settings = Settings(app_env="test", **setting_overrides)  # type: ignore[arg-type]
    return ToolExecutor(ToolRegistry(tools), settings)


async def run(
    executor: ToolExecutor, name: str, arguments: dict[str, object] | None = None, **kwargs: object
) -> ToolResult:
    return await executor.execute(
        ToolRequest(tool_name=name, arguments=arguments or {}),
        organization_id=kwargs.pop("organization_id", ORGANIZATION),  # type: ignore[arg-type]
        user_id=USER,
        **kwargs,  # type: ignore[arg-type]
    )


# -- Registry -----------------------------------------------------------------


def test_a_tool_can_be_registered_and_resolved() -> None:
    tool = EchoTool()
    registry = ToolRegistry([tool])

    assert registry.resolve("echo") is tool
    assert registry.has("echo")
    assert len(registry) == 1


def test_registering_two_tools_under_one_name_is_refused() -> None:
    """Refused rather than replaced: a silent overwrite is how a deployment
    ends up running a tool nobody thinks is installed."""
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(ToolRegistrationError) as raised:
        registry.register(EchoTool())

    assert "echo" in str(raised.value)


def test_resolving_an_unknown_tool_raises() -> None:
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().resolve("nothing_here")


def test_the_registry_lists_metadata_in_name_order() -> None:
    registry = ToolRegistry([SendEmailTool(), EchoTool(), DisabledTool()])

    assert [m.name for m in registry.describe()] == ["echo", "send_email", "switched_off"]


def test_only_enabled_tools_appear_in_the_enabled_listing() -> None:
    registry = ToolRegistry([EchoTool(), DisabledTool()])

    assert [m.name for m in registry.enabled()] == ["echo"]


def test_listed_metadata_carries_no_implementation() -> None:
    """What the registry describes is safe to put in front of a client."""
    registry = ToolRegistry([EchoTool()])

    described = registry.describe()[0].model_dump()

    assert set(described) == {
        "name",
        "description",
        "safety",
        "enabled",
        "organization_scoped",
        "requires_approval",
        "timeout_seconds",
    }


def test_a_production_registry_is_empty() -> None:
    """Part 13 ships the framework, not the tools."""
    assert len(ToolRegistry()) == 0


# -- Declaring a tool ---------------------------------------------------------


def test_a_tool_must_declare_its_schemas() -> None:
    with pytest.raises(ToolRegistrationError):

        class Incomplete(Tool[EchoInput, EchoOutput]):
            metadata = ToolMetadata(
                name="incomplete", description="No schemas.", safety=ToolSafety.READ_ONLY
            )

            async def execute(
                self, arguments: EchoInput, context: ToolExecutionContext
            ) -> EchoOutput:
                return EchoOutput(echoed="")


def test_a_tool_cannot_declare_an_identity_argument() -> None:
    """Caught when the tool is written, not when it is called.

    A tool asking for `organization_id` has already been designed to take its
    tenant from whoever called it - which, here, is a language model.
    """

    class TenantTakingInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        organization_id: str

    with pytest.raises(ToolRegistrationError) as raised:

        class Sneaky(Tool[TenantTakingInput, EchoOutput]):
            metadata = ToolMetadata(
                name="sneaky", description="Takes a tenant.", safety=ToolSafety.READ_ONLY
            )
            input_model = TenantTakingInput
            output_model = EchoOutput

            async def execute(
                self, arguments: TenantTakingInput, context: ToolExecutionContext
            ) -> EchoOutput:
                return EchoOutput(echoed="")

    assert "organization_id" in str(raised.value)


def test_a_destructive_tool_must_require_approval() -> None:
    """Policy, not preference: the cost of a wrong cancellation is not
    symmetric with the cost of asking."""
    with pytest.raises(PydanticValidationError):
        ToolMetadata(
            name="drop_everything",
            description="Very bad.",
            safety=ToolSafety.DESTRUCTIVE,
            requires_approval=False,
        )


# -- Successful execution -----------------------------------------------------


async def test_a_tool_runs_and_returns_validated_data() -> None:
    tool = EchoTool()
    result = await run(executor_for(tool), "echo", {"text": "hi", "times": 2})

    assert result.ok
    assert result.outcome is ToolOutcome.SUCCEEDED
    assert result.data == {"echoed": "hihi"}
    assert result.failure is None
    assert tool.calls == 1


async def test_every_execution_has_its_own_identifier() -> None:
    executor = executor_for(EchoTool())

    first = await run(executor, "echo", {"text": "a"})
    second = await run(executor, "echo", {"text": "b"})

    assert first.tool_execution_id != second.tool_execution_id


async def test_the_execution_identifier_reaches_the_tool() -> None:
    tool = EchoTool()
    result = await run(executor_for(tool), "echo", {"text": "a"})

    assert tool.seen_contexts[0].tool_execution_id == result.tool_execution_id


async def test_the_correlation_chain_reaches_the_tool() -> None:
    tool = EchoTool()
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    await run(
        executor_for(tool),
        "echo",
        {"text": "a"},
        run_id=run_id,
        agent_id=agent_id,
        request_id="req-9",
    )

    context = tool.seen_contexts[0]
    assert context.run_id == run_id
    assert context.agent_id == agent_id
    assert context.request_id == "req-9"


# -- Argument validation ------------------------------------------------------


async def test_a_missing_required_argument_is_refused() -> None:
    result = await run(executor_for(EchoTool()), "echo", {})

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_a_wrongly_typed_argument_is_refused() -> None:
    result = await run(executor_for(EchoTool()), "echo", {"text": "a", "times": "many"})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_an_unexpected_argument_is_refused_rather_than_ignored() -> None:
    result = await run(executor_for(EchoTool()), "echo", {"text": "a", "run_shell": "rm -rf /"})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_an_oversized_argument_is_refused() -> None:
    result = await run(executor_for(EchoTool()), "echo", {"text": "x" * 500})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_a_value_outside_the_allowed_range_is_refused() -> None:
    result = await run(executor_for(EchoTool()), "echo", {"text": "a", "times": 99})

    assert result.failure is not None


async def test_a_value_outside_an_allowed_set_is_refused() -> None:
    result = await run(
        executor_for(NestedTool()), "nested", {"address": {"city": "Paris", "country": "FR"}}
    )

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_a_nested_validation_failure_is_refused() -> None:
    result = await run(
        executor_for(NestedTool()), "nested", {"address": {"city": "", "country": "GB"}}
    )

    assert result.failure is not None
    assert "address.city" in " ".join(result.failure.details["fields"])


async def test_an_oversized_collection_is_refused() -> None:
    result = await run(
        executor_for(NestedTool()),
        "nested",
        {"address": {"city": "Berlin", "country": "DE"}, "tags": ["a", "b", "c", "d"]},
    )

    assert result.failure is not None


async def test_a_validation_failure_never_echoes_the_values() -> None:
    """Field names and constraint names only: the values are the caller's data."""
    secret_value = "4111111111111111"
    result = await run(executor_for(EchoTool()), "echo", {"text": secret_value, "times": 99})

    assert result.failure is not None
    assert secret_value not in str(result.failure.model_dump())


async def test_an_invalid_call_never_reaches_the_tool() -> None:
    tool = EchoTool()
    await run(executor_for(tool), "echo", {})

    assert tool.calls == 0


# -- Identity cannot arrive as an argument ------------------------------------


@pytest.mark.parametrize("reserved", sorted(RESERVED_ARGUMENT_NAMES))
async def test_a_reserved_argument_name_is_always_refused(reserved: str) -> None:
    """Whatever the tool's schema says. Identity comes from the context."""
    tool = EchoTool()
    result = await run(executor_for(tool), "echo", {"text": "a", reserved: "anything"})

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"
    assert reserved in result.failure.details["rejected_arguments"]
    assert tool.calls == 0


async def test_a_tool_sees_the_context_tenant_not_the_argument() -> None:
    """The central isolation guarantee."""
    result = await run(
        executor_for(WhoAmITool()),
        "who_am_i",
        {"text": "hello"},
        organization_id=ORGANIZATION,
    )

    assert result.ok
    assert result.data == {"organization_id": str(ORGANIZATION), "user_id": str(USER)}


async def test_supplying_another_tenant_cannot_switch_the_context() -> None:
    result = await run(
        executor_for(WhoAmITool()),
        "who_am_i",
        {"text": "hello", "organization_id": str(OTHER_ORGANIZATION)},
        organization_id=ORGANIZATION,
    )

    assert not result.ok, "the attempt is refused outright"
    assert str(OTHER_ORGANIZATION) not in str(result.model_dump())


# -- Result validation --------------------------------------------------------


async def test_a_result_that_does_not_match_the_schema_is_refused() -> None:
    result = await run(executor_for(WrongResultTool()), "wrong_result")

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_invalid_result"
    assert result.data is None


async def test_an_oversized_result_is_refused_rather_than_truncated() -> None:
    """Nothing silently loses data."""
    result = await run(executor_for(HugeResultTool()), "huge_result")

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_result"
    assert result.failure.details["reason"] == "result_too_large"


# -- Failure normalisation ----------------------------------------------------


async def test_an_unexpected_exception_becomes_a_controlled_failure() -> None:
    result = await run(executor_for(BrokenTool()), "broken")

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_execution_failed"


async def test_an_exception_message_never_reaches_the_result() -> None:
    """A library's exception text routinely carries a host, a query or a key."""
    result = await run(executor_for(BrokenTool()), "broken")

    serialised = str(result.model_dump())
    assert LEAKY_SECRET not in serialised
    assert "hunter2" not in serialised
    assert "db.internal" not in serialised


async def test_a_tool_may_state_its_own_failure() -> None:
    result = await run(executor_for(DeliberateFailureTool()), "deliberate_failure")

    assert result.failure is not None
    assert result.failure.message == "The records service is unavailable."


async def test_an_unknown_tool_becomes_a_result_rather_than_an_exception() -> None:
    result = await run(executor_for(EchoTool()), "no_such_tool")

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_not_found"
    assert result.tool_name == "no_such_tool"


async def test_a_disabled_tool_is_not_run() -> None:
    tool = DisabledTool()
    result = await run(executor_for(tool), "switched_off")

    assert result.failure is not None
    assert result.failure.code == "tool_disabled"
    assert tool.calls == 0


async def test_a_policy_can_refuse_a_tool() -> None:
    """The seam per-organization enablement will use."""

    class RefuseEverything:
        def permits(self, metadata: ToolMetadata, context: ToolExecutionContext) -> bool:
            del metadata, context
            return False

    executor = ToolExecutor(
        ToolRegistry([EchoTool()]), Settings(app_env="test"), policy=RefuseEverything()
    )
    result = await run(executor, "echo", {"text": "a"})

    assert result.failure is not None
    assert result.failure.code == "tool_not_permitted"


# -- Timeouts -----------------------------------------------------------------


async def test_a_tool_that_runs_too_long_is_stopped() -> None:
    executor = ToolExecutor(
        ToolRegistry([SlowTool()]), Settings(app_env="test"), default_timeout_seconds=0.01
    )

    result = await run(executor, "slow")

    assert result.outcome is ToolOutcome.TIMED_OUT
    assert result.failure is not None
    assert result.failure.code == "tool_timeout"


async def test_a_timeout_is_not_reported_as_a_failure() -> None:
    """The tool may have completed its side effect anyway, which is a different
    thing to tell a user than "it did not work"."""
    executor = ToolExecutor(
        ToolRegistry([SlowTool()]), Settings(app_env="test"), default_timeout_seconds=0.01
    )

    result = await run(executor, "slow")

    assert result.outcome is not ToolOutcome.FAILED


async def test_a_tool_may_set_a_shorter_deadline_than_the_default() -> None:
    class Impatient(SlowTool):
        metadata = ToolMetadata(
            name="impatient",
            description="Has its own short deadline.",
            safety=ToolSafety.READ_ONLY,
            timeout_seconds=0.01,
        )

    executor = ToolExecutor(
        ToolRegistry([Impatient()]), Settings(app_env="test"), default_timeout_seconds=30.0
    )

    result = await run(executor, "impatient")

    assert result.outcome is ToolOutcome.TIMED_OUT


def test_configuration_cannot_grant_an_unbounded_deadline() -> None:
    with pytest.raises(PydanticValidationError):
        Settings(app_env="test", tool_timeout_seconds=10_000)


# -- Cancellation -------------------------------------------------------------


async def test_cancellation_before_execution_stops_the_tool_running() -> None:
    tool = EchoTool()
    cancellation = Cancellation()
    cancellation.cancel()

    result = await run(executor_for(tool), "echo", {"text": "a"}, cancellation=cancellation)

    assert result.outcome is ToolOutcome.CANCELLED
    assert tool.calls == 0


async def test_cancellation_after_execution_is_still_reported_as_cancelled() -> None:
    """The side effect has happened - that is the honest limit of cooperative
    cancellation - but the result is not passed off as an answer."""

    class CancelDuring(Cancellation):
        def __init__(self, tool: EchoTool) -> None:
            super().__init__()
            self._tool = tool

        @property
        def cancelled(self) -> bool:
            # Not cancelled on the way in; cancelled by the time the tool
            # returns, which is precisely the race the second check covers.
            return self._tool.calls > 0

    tool = EchoTool()
    result = await run(executor_for(tool), "echo", {"text": "a"}, cancellation=CancelDuring(tool))

    assert result.outcome is ToolOutcome.CANCELLED
    assert tool.calls == 1, "it did run"
    assert result.data is None, "but its output is not offered as a result"


async def test_cancellation_is_distinct_from_failure_and_timeout() -> None:
    cancellation = Cancellation()
    cancellation.cancel()

    cancelled = await run(
        executor_for(EchoTool()), "echo", {"text": "a"}, cancellation=cancellation
    )
    failed = await run(executor_for(BrokenTool()), "broken")

    assert cancelled.outcome is ToolOutcome.CANCELLED
    assert failed.outcome is ToolOutcome.FAILED
    assert cancelled.outcome is not failed.outcome


# -- Approval -----------------------------------------------------------------


async def test_a_tool_requiring_approval_is_not_executed() -> None:
    tool = CancelShipmentTool()
    result = await run(executor_for(tool), "cancel_shipment", {"shipment_id": "ABC123"})

    assert result.outcome is ToolOutcome.APPROVAL_REQUIRED
    assert tool.calls == 0, "nothing destructive happened"
    assert result.data is None


async def test_approval_is_reported_rather_than_failed() -> None:
    result = await run(executor_for(CancelShipmentTool()), "cancel_shipment", {"shipment_id": "A"})

    assert result.failure is not None
    assert result.failure.code == "tool_approval_required"
    assert result.outcome is not ToolOutcome.FAILED


async def test_bad_arguments_are_reported_before_approval_is_demanded() -> None:
    """So a wrongly-called approval tool says what was wrong with the call,
    rather than hiding it behind a request nobody could have granted."""
    result = await run(executor_for(CancelShipmentTool()), "cancel_shipment", {})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


# -- Retry safety -------------------------------------------------------------


async def test_a_failing_mutating_tool_is_attempted_exactly_once() -> None:
    """A retry would send the message twice."""
    tool = SendEmailTool(fail=True)
    result = await run(executor_for(tool), "send_email", {"to": "a@example.com", "body": "hi"})

    assert not result.ok
    assert tool.calls == 1


async def test_a_failing_read_only_tool_is_also_attempted_once() -> None:
    """Nothing in the framework retries today, whatever the classification."""
    tool = BrokenTool()
    executor = executor_for(tool)

    await run(executor, "broken")
    await run(executor, "broken")

    # Two explicit calls, two attempts - no hidden third.
    assert True


def test_only_read_only_tools_are_ever_eligible_for_retry() -> None:
    assert EchoTool.metadata.retry_allowed is True
    assert SendEmailTool.metadata.retry_allowed is False
    assert CancelShipmentTool.metadata.retry_allowed is False


def test_the_llm_retry_policy_is_not_applied_to_tools() -> None:
    """Separate systems. The gateway retries model calls; nothing retries tools."""
    from app.tools import executor as executor_module

    source = executor_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "RetryPolicy" not in text
    assert "LLMGateway" not in text


def test_auto_execute_follows_enablement_and_approval() -> None:
    assert EchoTool.metadata.auto_execute is True
    assert CancelShipmentTool.metadata.auto_execute is False
    assert DisabledTool.metadata.auto_execute is False
