"""Example tools for testing the framework.

Deliberately not in ``app/tools/``: Part 13 ships no real tools, and a fixture
that lives in the application is a fixture somebody eventually registers in
production. These exist only to give the executor something to execute.

None of them touches a network, a database or a credential.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.base import Tool
from app.tools.exceptions import ToolExecutionError
from app.tools.models import ToolExecutionContext, ToolMetadata, ToolSafety

# A plausible-looking secret, so a test can assert it never escapes.
LEAKY_SECRET = "postgres://admin:hunter2@db.internal:5432/records"


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100)
    times: int = Field(default=1, ge=1, le=5)


class EchoOutput(BaseModel):
    echoed: str


class EchoTool(Tool[EchoInput, EchoOutput]):
    """Succeeds, and records what it was given."""

    metadata = ToolMetadata(
        name="echo",
        description="Repeats the text it is given.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EchoInput
    output_model = EchoOutput

    def __init__(self) -> None:
        self.calls = 0
        self.seen_contexts: list[ToolExecutionContext] = []

    async def execute(self, arguments: EchoInput, context: ToolExecutionContext) -> EchoOutput:
        self.calls += 1
        self.seen_contexts.append(context)
        return EchoOutput(echoed=arguments.text * arguments.times)


class TenantOutput(BaseModel):
    organization_id: str
    user_id: str


class WhoAmITool(Tool[EchoInput, TenantOutput]):
    """Reports the identity it was handed, so a test can check where it came from."""

    metadata = ToolMetadata(
        name="who_am_i",
        description="Reports the tenant this execution acts for.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EchoInput
    output_model = TenantOutput

    async def execute(self, arguments: EchoInput, context: ToolExecutionContext) -> TenantOutput:
        del arguments
        # Read from the context, never from the arguments. That is the rule the
        # framework exists to make easy.
        return TenantOutput(
            organization_id=str(context.organization_id), user_id=str(context.user_id)
        )


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlowTool(Tool[EmptyInput, EchoOutput]):
    """Sleeps longer than any deadline a test will give it."""

    metadata = ToolMetadata(
        name="slow",
        description="Takes longer than it is allowed to.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EmptyInput
    output_model = EchoOutput

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds
        self.completed = False

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> EchoOutput:
        del arguments, context
        await asyncio.sleep(self.seconds)
        self.completed = True
        return EchoOutput(echoed="finished")


class BrokenTool(Tool[EmptyInput, EchoOutput]):
    """Raises an exception whose text must never reach a client."""

    metadata = ToolMetadata(
        name="broken",
        description="Fails with a message full of things nobody should see.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EmptyInput
    output_model = EchoOutput

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> EchoOutput:
        del arguments, context
        raise RuntimeError(f"connection failed: {LEAKY_SECRET}")


class DeliberateFailureTool(Tool[EmptyInput, EchoOutput]):
    """Fails on purpose, with its own client-safe message."""

    metadata = ToolMetadata(
        name="deliberate_failure",
        description="Raises a tool error of its own.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EmptyInput
    output_model = EchoOutput

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> EchoOutput:
        del arguments, context
        raise ToolExecutionError("The records service is unavailable.")


class WrongResultTool(Tool[EmptyInput, EchoOutput]):
    """Returns something its own output schema does not describe."""

    metadata = ToolMetadata(
        name="wrong_result",
        description="Returns the wrong shape.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EmptyInput
    output_model = EchoOutput

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> EchoOutput:
        del arguments, context
        return {"not_the_schema": object()}  # type: ignore[return-value]


class HugeOutput(BaseModel):
    blob: str


class HugeResultTool(Tool[EmptyInput, HugeOutput]):
    """Returns far more than the framework will carry."""

    metadata = ToolMetadata(
        name="huge_result",
        description="Returns an oversized payload.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = EmptyInput
    output_model = HugeOutput

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> HugeOutput:
        del arguments, context
        return HugeOutput(blob="x" * 100_000)


class DisabledTool(Tool[EmptyInput, EchoOutput]):
    """Registered but switched off."""

    metadata = ToolMetadata(
        name="switched_off",
        description="Exists, but is disabled.",
        safety=ToolSafety.READ_ONLY,
        enabled=False,
    )
    input_model = EmptyInput
    output_model = EchoOutput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: EmptyInput, context: ToolExecutionContext) -> EchoOutput:
        del arguments, context
        self.calls += 1
        return EchoOutput(echoed="should never run")


class SendEmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(max_length=320)
    body: str = Field(max_length=1_000)


class SendEmailTool(Tool[SendEmailInput, EchoOutput]):
    """Has an outside effect, so it must never be retried automatically."""

    metadata = ToolMetadata(
        name="send_email",
        description="Sends a message. Pretends to.",
        safety=ToolSafety.MUTATING,
    )
    input_model = SendEmailInput
    output_model = EchoOutput

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def execute(self, arguments: SendEmailInput, context: ToolExecutionContext) -> EchoOutput:
        del context
        self.calls += 1
        if self.fail:
            raise RuntimeError("smtp refused the message")
        return EchoOutput(echoed=arguments.to)


class CancelShipmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipment_id: str = Field(max_length=64)


class CancelShipmentTool(Tool[CancelShipmentInput, EchoOutput]):
    """Destructive, and therefore approval-gated."""

    metadata = ToolMetadata(
        name="cancel_shipment",
        description="Cancels a shipment. Pretends to.",
        safety=ToolSafety.DESTRUCTIVE,
        requires_approval=True,
    )
    input_model = CancelShipmentInput
    output_model = EchoOutput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self, arguments: CancelShipmentInput, context: ToolExecutionContext
    ) -> EchoOutput:
        del context
        self.calls += 1
        return EchoOutput(echoed=arguments.shipment_id)


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=50)
    country: Literal["GB", "US", "DE"]


class NestedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: Address
    tags: list[str] = Field(default_factory=list, max_length=3)


class NestedTool(Tool[NestedInput, EchoOutput]):
    """Exercises nested and collection validation."""

    metadata = ToolMetadata(
        name="nested",
        description="Takes a nested structure.",
        safety=ToolSafety.READ_ONLY,
    )
    input_model = NestedInput
    output_model = EchoOutput

    async def execute(self, arguments: NestedInput, context: ToolExecutionContext) -> EchoOutput:
        del context
        return EchoOutput(echoed=arguments.address.city)


def an_execution_context(**overrides: object) -> ToolExecutionContext:
    defaults: dict[str, object] = {
        "tool_execution_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
    }
    return ToolExecutionContext(**{**defaults, **overrides})  # type: ignore[arg-type]
