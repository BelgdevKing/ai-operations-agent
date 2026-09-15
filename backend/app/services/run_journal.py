"""The database behind the run journal.

    AgentRunner -> RunJournal (protocol) -> DatabaseRunJournal -> PostgreSQL

This is the only place agent execution becomes SQL. The runtime holds the
protocol and never sees a session, so the loop stays a loop and the question
"what happens if this transaction is open too long?" has exactly one file to
read.

**Short transactions, on purpose.** Every method commits what it wrote before
returning. A run makes model calls that take seconds and tool calls that reach
other systems, and holding a transaction across either would pin a connection
and a set of row locks for the whole time. The cost of committing as we go is
that a run which dies halfway leaves a partial record - which is the *point*:
a partial record is what makes the run recoverable, and an all-or-nothing
transaction would leave nothing at all.

**Metadata here, content there.** ``agent_runs``, ``agent_steps`` and
``tool_executions`` get names, counts, codes and timings. ``messages`` gets the
turns. Nothing in this file writes a tool's arguments or output into an
operational table, and nothing writes them into ``approvals.parameters``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.journal import RecordedTurn, ToolAttempt
from app.agents.models import AgentRun
from app.agents.models import AgentStep as RuntimeStep
from app.models.agent_run import AgentRunRecord, AgentStepRecord, ToolExecutionRecord
from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.repositories.agent_run import ToolExecutionRepository
from app.repositories.conversation import ConversationRepository
from app.tools.models import ToolMetadata
from app.tools.registry import ToolRegistry
from app.tools.summary import ActionSummary, build_summary

logger = logging.getLogger(__name__)

# What an approver is told, built from the tool's own classification. Never from
# its arguments: an approver needs to know *what kind of thing* is being asked
# for, and the arguments are the tenant's business data.
_REASON = (
    "The agent asked to run {tool}, which is classified {safety} and cannot be "
    "performed without a person agreeing to it."
)


class DatabaseRunJournal:
    """Writes one run down as it happens.

    Bound to a single run and a single conversation, both of which the
    orchestration layer created before the run started. That is why nothing here
    takes an organization from its arguments: it was fixed when the journal was
    built, from the caller's verified membership.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        record: AgentRunRecord,
        conversation_id: uuid.UUID,
        registry: ToolRegistry | None = None,
        approval_expires_after: timedelta | None = None,
    ) -> None:
        self._session = session
        self._organization_id = organization_id
        self._record = record
        self._conversation_id = conversation_id
        self._registry = registry
        # A duration rather than a Settings object: this is the only
        # configuration the journal has ever needed, and taking the whole of
        # settings to read one number would make every future settings change a
        # question about the write path for runs.
        self._approval_expires_after = approval_expires_after

        self._executions = ToolExecutionRepository(session, organization_id)
        self._conversations = ConversationRepository(session, organization_id)

    # -- Run state -------------------------------------------------------------

    async def record_state(self, run: AgentRun) -> None:
        """Bring the durable row in line with the working copy.

        Also what makes abandonment detectable: every call touches the row, so
        ``updated_at`` is the last time this run made progress, not the last time
        anybody looked at it.
        """
        record = self._record

        record.status = run.status
        record.step_count = run.step_count
        record.tool_call_count = run.tool_call_count
        record.input_tokens = run.usage.input_tokens
        record.output_tokens = run.usage.output_tokens
        record.latency_ms = round(run.latency_ms)
        record.error_code = run.error_code
        record.error_message = _truncate(run.error_message)
        record.started_at = run.started_at
        record.completed_at = run.completed_at
        record.conversation_id = self._conversation_id

        # A status change alone would not move updated_at if nothing else
        # differed, so it is set explicitly rather than left to onupdate.
        record.updated_at = datetime.now(UTC)

        await self._session.commit()

    # -- Steps -----------------------------------------------------------------

    async def record_step(self, run: AgentRun, step: RuntimeStep) -> None:
        """One model call.

        The decision's *kind* and, for a tool request, its name. Never the
        answer's text and never the arguments - both are turns, and turns go to
        the conversation.
        """
        self._session.add(
            AgentStepRecord(
                organization_id=self._organization_id,
                run_id=run.id,
                step_number=step.number,
                decision_type=str(step.decision.type.value),
                tool_name=getattr(step.decision, "tool_name", None),
                model=step.model,
                input_tokens=step.usage.input_tokens,
                output_tokens=step.usage.output_tokens,
                latency_ms=round(step.latency_ms),
                message_count=step.message_count,
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
        )
        await self.record_state(run)

    # -- Tool executions -------------------------------------------------------

    async def record_tool(self, run: AgentRun, attempt: ToolAttempt) -> None:
        """One tool attempt, keyed by the execution id the framework assigned.

        An update when the row already exists - which is what a resumed run does
        after an approval: the same execution, now with an outcome. The
        ``executed`` flag is never lowered here, because the claim that raised it
        is the at-most-once guarantee and this method must not be able to undo
        one.
        """
        result = attempt.result
        metadata = self._metadata(attempt.tool_name)

        existing = await self._executions.get(result.tool_execution_id)

        if existing is None:
            existing = ToolExecutionRecord(
                id=result.tool_execution_id,
                organization_id=self._organization_id,
                run_id=run.id,
                step_number=attempt.step_number,
                tool_name=attempt.tool_name,
                safety=metadata.safety.value if metadata else None,
                requires_approval=bool(metadata.requires_approval) if metadata else False,
                argument_count=attempt.argument_count,
            )
            self._session.add(existing)

        # Raised, never lowered. The claim that set this flag is the
        # at-most-once guarantee, and a later write must not be able to undo one.
        if attempt.executed:
            existing.executed = True

        existing.outcome = result.outcome.value
        existing.error_code = result.failure.code if result.failure else None
        existing.result_field_count = len(result.data) if result.data else None
        existing.duration_ms = round(result.duration_ms)
        existing.completed_at = datetime.now(UTC)

        await self._session.commit()

    # -- Conversation ----------------------------------------------------------

    async def record_turns(self, run: AgentRun, turns: Sequence[RecordedTurn]) -> None:
        """Store conversation content, in the order it happened."""
        del run

        if not turns:
            return

        await self._conversations.append_all(
            self._conversation_id, [(turn.message, turn.payload) for turn in turns]
        )
        await self._session.commit()

    # -- Approvals -------------------------------------------------------------

    async def record_approval_request(
        self, run: AgentRun, attempt: ToolAttempt, arguments: Mapping[str, Any]
    ) -> None:
        """Put the execution in front of a person.

        ``parameters`` is left at its empty default. The arguments the agent
        proposed are in the conversation, stored beside the turn that proposed
        them; copying them here would put business data into an operational
        record for no benefit, since an approval authorises an *execution* by its
        id rather than a set of values.

        What *is* written from them is the summary, and only through the tool's
        own declaration: ``ApprovalSummary`` names which of its fields a reviewer
        may see, and this is where that allow-list is applied. A tool that
        declares nothing leaves both summary columns empty and the approval says
        no more than it did before - safe, and not very useful, which is why a
        gated tool should declare one.

        The reason is the *framework's*, built from the tool's safety
        classification. It is deliberately not anything the model said: a model
        does not decide what needs approving, and a sentence it wrote about its
        own authority is not an authorization record.

        ``agent_id`` is left null for the same reason ``conversations.agent_id``
        is: the agent came from the server-side registry, not the ``agents``
        table. The run this approval points at records which agent it was.
        """
        metadata = self._metadata(attempt.tool_name)
        safety = metadata.safety.value if metadata else "sensitive"

        summary: ActionSummary | None = None
        if metadata is not None and metadata.approval_summary is not None:
            summary = build_summary(metadata.approval_summary, arguments)

        self._session.add(
            Approval(
                organization_id=self._organization_id,
                requested_by=run.user_id,
                conversation_id=self._conversation_id,
                run_id=run.id,
                tool_execution_id=attempt.result.tool_execution_id,
                tool_name=attempt.tool_name,
                action=attempt.tool_name,
                reason=_REASON.format(tool=attempt.tool_name, safety=safety),
                summary=summary.headline if summary else None,
                summary_fields=summary.as_dicts() if summary else [],
                expires_at=self._expires_at(),
                status=ApprovalStatus.PENDING,
            )
        )
        await self._session.commit()

        logger.info(
            "Approval requested",
            extra={
                "context": {
                    "run_id": str(run.id),
                    "organization_id": str(self._organization_id),
                    "tool": attempt.tool_name,
                    "tool_execution_id": str(attempt.result.tool_execution_id),
                    "safety": safety,
                }
            },
        )

    # -- Helpers ---------------------------------------------------------------

    def _expires_at(self) -> datetime | None:
        """When this approval stops being decidable.

        Stamped now, from the deadline configured now, so that changing the
        setting later never moves a deadline somebody has already been given.
        ``None`` where no expiry is configured, which the decision path reads as
        "waits indefinitely" - the behaviour before expiry existed.
        """
        if self._approval_expires_after is None:
            return None
        return datetime.now(UTC) + self._approval_expires_after

    def _metadata(self, tool_name: str) -> ToolMetadata | None:
        """What the registry says about a tool, or nothing if it has none.

        Tolerant on purpose: a run that asked for a tool which does not exist
        still produces a durable record of having asked.
        """
        if self._registry is None or not self._registry.has(tool_name):
            return None
        return self._registry.resolve(tool_name).metadata


def _truncate(message: str | None) -> str | None:
    """Keep an error message inside its column.

    These are the error classes' own client-safe sentences, so truncation should
    never actually trigger; it is here so that a longer one added later cannot
    turn a failed run into a failed *write*, which would lose the record of the
    failure entirely.
    """
    if message is None:
        return None
    return message[:500]
