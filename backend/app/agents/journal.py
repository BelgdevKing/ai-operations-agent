"""Where a run writes itself down, without knowing where that is.

The runtime is not allowed to contain data access. It is also not allowed to be
the only record of what it did - a request that dies mid-run must leave
something behind. Those two requirements meet here: the runner calls a narrow
protocol as it goes, and something outside it decides that "write this down"
means an ``INSERT`` inside a short transaction.

    AgentRunner  ->  RunJournal  ->  (app.services.run_journal)  ->  PostgreSQL

Nothing in this module imports SQLAlchemy, a session, or a model. The default
implementation writes nowhere at all, which is what keeps every unit test that
predates persistence working unchanged.

Two kinds of thing are recorded, and the difference is the whole security
design of the durable layer:

**Operational metadata** - a step happened, a tool was attempted, a run changed
state. Counts, names, codes and timings. This is what ``agent_runs``,
``agent_steps`` and ``tool_executions`` hold.

**Conversation content** - the turns themselves, including the tool request the
agent made and the tool result it was given. This is what ``messages`` holds,
and it is the only place a tool's arguments or output are allowed to be stored,
because it is the only store whose purpose is to hold the tenant's own words.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.agents.decisions import ToolRequestDecision
from app.agents.models import AgentRun, AgentStep
from app.ai.models import LLMMessage, LLMToolResult
from app.tools.models import ToolResult

# Discriminators for the structured form of a rendered turn.
TURN_TOOL_REQUEST = "tool_request"
TURN_TOOL_RESULT = "tool_result"


class RecordedTurn(BaseModel):
    """One conversation turn on its way to being stored.

    ``payload`` is the structured form of a turn whose text the runtime
    rendered - a tool request or a tool result. Without it a stored conversation
    could only be replayed by parsing its own rendered text back out, which is
    the kind of thing that works until a tool returns a string containing a
    closing marker.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: LLMMessage
    payload: dict[str, Any] | None = None


class ToolAttempt(BaseModel):
    """What the framework did about one tool the agent asked for.

    Metadata only, and deliberately so: ``argument_count`` rather than the
    arguments, and the result's outcome rather than the result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_number: int = Field(ge=1)
    tool_name: str
    argument_count: int = Field(ge=0)
    executed: bool = Field(
        description="Whether the tool's body actually ran. False for a refused, "
        "pending or rejected execution."
    )
    result: ToolResult


class RunJournal(Protocol):
    """Everything a run needs somewhere durable to put.

    Every method is expected to be its own unit of work. A run makes model calls
    between them, and a transaction may not be held open across one.
    """

    async def record_state(self, run: AgentRun) -> None:
        """The run changed status, or finished a step's worth of bookkeeping."""
        ...

    async def record_step(self, run: AgentRun, step: AgentStep) -> None:
        """One model call completed."""
        ...

    async def record_tool(self, run: AgentRun, attempt: ToolAttempt) -> None:
        """One tool was attempted, whatever came of it."""
        ...

    async def record_turns(self, run: AgentRun, turns: Sequence[RecordedTurn]) -> None:
        """Conversation content the run produced, in order."""
        ...

    async def record_approval_request(
        self, run: AgentRun, attempt: ToolAttempt, arguments: Mapping[str, Any]
    ) -> None:
        """A person needs to decide about *attempt* before the run can continue.

        *arguments* is the only place in this protocol where the values a model
        proposed are handed over, and it is handed over to be **reduced**: the
        implementation projects it through the tool's own declared allow-list
        and stores that, never the mapping itself. It is a parameter rather than
        a field of :class:`ToolAttempt` precisely so that distinction survives -
        an attempt stays metadata-only wherever else it travels.
        """
        ...


class NullJournal:
    """A journal that keeps nothing.

    The default, so the runtime can be exercised with no database at all - which
    is what the unit suite does. Silent rather than raising: a run that is not
    being persisted is a legitimate configuration, not an error.
    """

    async def record_state(self, run: AgentRun) -> None:
        del run

    async def record_step(self, run: AgentRun, step: AgentStep) -> None:
        del run, step

    async def record_tool(self, run: AgentRun, attempt: ToolAttempt) -> None:
        del run, attempt

    async def record_turns(self, run: AgentRun, turns: Sequence[RecordedTurn]) -> None:
        del run, turns

    async def record_approval_request(
        self, run: AgentRun, attempt: ToolAttempt, arguments: Mapping[str, Any]
    ) -> None:
        del run, attempt, arguments


NULL_JOURNAL: RunJournal = NullJournal()
"""The journal used when nothing is being persisted."""


# -- The structured form of a rendered turn -----------------------------------


def tool_request_payload(
    decision: ToolRequestDecision, *, tool_execution_id: uuid.UUID | None
) -> dict[str, Any]:
    """The structured form of the assistant turn that asked for a tool.

    The arguments are here because this payload is stored beside the rendered
    text of the same turn, in the conversation - which already contains them.
    They are what a resumed run replays, which is why they are kept structurally
    rather than re-parsed out of the text.
    """
    return {
        "type": TURN_TOOL_REQUEST,
        "tool_name": decision.tool_name,
        "arguments": decision.arguments,
        "tool_execution_id": str(tool_execution_id) if tool_execution_id else None,
    }


def tool_result_payload(result: LLMToolResult) -> dict[str, Any]:
    """The structured form of the tool turn.

    Carries the same fields the rendered text does. The duplication is
    intentional and confined to one row of the content store: the text is what a
    person reads and what the provider receives, and the payload is what a
    resumed run rebuilds the turn from without re-parsing that text.
    """
    return {
        "type": TURN_TOOL_RESULT,
        "tool_name": result.tool_name,
        "execution_id": result.execution_id,
        "succeeded": result.succeeded,
        "outcome": result.outcome,
        "data": result.data,
        "error": result.error,
    }


def tool_result_from_payload(payload: dict[str, Any]) -> LLMToolResult:
    """Rebuild a tool turn's result from what was stored."""
    return LLMToolResult(
        tool_name=str(payload["tool_name"]),
        execution_id=str(payload["execution_id"]),
        succeeded=bool(payload["succeeded"]),
        outcome=str(payload["outcome"]),
        data=payload.get("data"),
        error=payload.get("error"),
    )


def pending_request_from_payload(payload: dict[str, Any]) -> ToolRequestDecision:
    """Rebuild the tool request a paused run is waiting on.

    The arguments come back exactly as the agent proposed them, so an approved
    execution runs the call that was approved rather than one the model might
    produce a second time.
    """
    arguments = payload.get("arguments") or {}
    return ToolRequestDecision(
        tool_name=str(payload["tool_name"]),
        arguments=dict(arguments),
    )
