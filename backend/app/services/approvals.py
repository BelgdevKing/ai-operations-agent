"""Deciding an approval, and resuming the run that was waiting on it.

    POST /approvals/{id}/approve
        -> one conditional UPDATE decides who wins
        -> claim the execution, once
        -> run the tool with the decision in hand
        -> resume the SAME run, from the SAME conversation

Three guarantees, each held by the database rather than by a check in Python:

**One decision.** ``ApprovalRepository.decide`` moves the row out of ``pending``
in a single statement with the state in its ``WHERE`` clause. A second approve,
an approve after a reject, or two administrators clicking together - exactly one
changes a row, and everybody else is told it is already decided.

**One execution.** ``ToolExecutionRepository.claim`` raises ``executed`` with
``WHERE executed = false``. The tool body is reached at most once for that
execution id, whatever happens above it.

**One run.** Approval resumes the run that paused. Not a copy, not a successor -
the same ``run_id``, the same conversation, the same remaining step budget, so
the audit trail reads as one story with a gap in the middle where a person was
thinking.

Two kinds of process pause here, and an approval belongs to exactly one of them:
an **agent run**, which stopped because its own tool needed a person, and a
**workflow run**, which stopped at a step. They are decided identically - one
conditional update, one claim - and differ only in what is resumed afterwards.
An agent step *inside* a workflow is both at once: the agent run resumes first,
and then the workflow, because the workflow's next step reads what the agent
finally said.

A rejection is not a failure. It comes back as a ``REJECTED`` tool outcome, goes
into the conversation like any other result, and the bounded loop decides what
to say about it. Nothing is fabricated as success and nothing is dressed up as
an infrastructure fault.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cancellation import NEVER_CANCELLED, CancellationToken
from app.agents.decisions import ToolRequestDecision
from app.agents.exceptions import AgentCancelledError, AgentError
from app.agents.journal import ToolAttempt, pending_request_from_payload
from app.agents.models import AgentRun, AgentRunStatus
from app.agents.runner import reached_the_tool
from app.agents.runtime import AgentRuntime
from app.ai.exceptions import LLMError
from app.ai.models import LLMMessage
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.models.agent_run import AgentRunRecord
from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.models.organization import OrganizationMember
from app.repositories.agent_run import AgentRunRepository, ToolExecutionRepository
from app.repositories.approval import ApprovalRepository
from app.repositories.conversation import (
    ConversationRepository,
    pending_tool_request,
    to_llm_messages,
)
from app.services.agent_execution import AgentExecutionService, RunView, build_carryover
from app.services.audit import record_agent_run, record_approval_decision
from app.services.run_journal import DatabaseRunJournal
from app.services.workflow_execution import WorkflowRunView, WorkflowService
from app.tools.executor import ToolExecutor
from app.tools.models import ApprovalGrant, ToolRequest, ToolResult
from app.tools.registry import ToolRegistry
from app.workflows.exceptions import WorkflowRunNotFoundError

logger = logging.getLogger(__name__)


class ApprovalNotFoundError(NotFoundError):
    """No such approval for this organization.

    The same answer whether it does not exist or belongs to another tenant, so
    that guessing an id cannot confirm another organization's pending action.
    """

    code = "approval_not_found"
    message = "That approval request does not exist."


class ApprovalAlreadyDecidedError(ConflictError):
    """Somebody already decided this one.

    Raised when the conditional update changed no row: a second approval, an
    approval after a rejection, or a rejection after an approval. The important
    property is what does *not* happen - the tool is not run again, and the
    earlier decision is not overwritten.
    """

    code = "approval_already_decided"
    message = "That approval has already been decided."


class ApprovalNotResumableError(ConflictError):
    """The decision was recorded, but the run cannot continue.

    The run has moved on - abandoned, cancelled, or already finished - or the
    tool request it was waiting on is no longer in the conversation. The
    decision stands and is auditable; what cannot happen is guessing at the call
    that was approved.
    """

    code = "approval_not_resumable"
    message = "That approval was recorded, but its run can no longer continue."


@dataclass(frozen=True)
class ApprovalDecision:
    """What a decision did.

    Exactly one of the two runs is set for an ordinary approval, and both are
    for an agent step inside a workflow. Returned as a pair rather than as
    whichever happened to apply, so a typed client does not have to guess which
    shape came back.
    """

    approval: Approval
    agent_run: RunView | None = None
    workflow_run: WorkflowRunView | None = None


class ApprovalService:
    """Reads and decides approvals for one verified membership."""

    def __init__(
        self,
        session: AsyncSession,
        runtime: Callable[[], AgentRuntime],
        settings: Settings,
        membership: OrganizationMember,
        executor: ToolExecutor,
        tools: ToolRegistry | None = None,
        workflows: WorkflowService | None = None,
    ) -> None:
        self._session = session
        # A factory rather than a runtime: building one builds the LLM gateway,
        # which fails on a deployment with no provider credential. Reading the
        # queue and deciding a *workflow* approval never need a model, and
        # answering either with a 500 about a provider would be a lie about what
        # went wrong. The agent path calls this; nothing else does.
        self._runtime = runtime
        self._settings = settings
        self._executor = executor
        self._tools = tools
        # Absent only where nothing constructs one - the API always supplies it.
        # A workflow approval arriving without it is reported as unresumable
        # rather than silently decided and abandoned.
        self._workflows = workflows

        # From the verified membership, once. Every query below is scoped to it.
        self._organization_id = membership.organization_id
        self._user_id = membership.user_id
        self._membership = membership

        self._approvals = ApprovalRepository(session, self._organization_id)
        self._runs = AgentRunRepository(session, self._organization_id)
        self._executions = ToolExecutionRepository(session, self._organization_id)
        self._conversations = ConversationRepository(session, self._organization_id)

    # -- Reading ---------------------------------------------------------------

    async def list_pending(self, *, limit: int = 50) -> Sequence[Approval]:
        """This organization's approval queue."""
        return await self._approvals.list_pending(limit=limit)

    async def get(self, approval_id: uuid.UUID) -> Approval:
        """One of this organization's approvals."""
        approval = await self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalNotFoundError()
        return approval

    # -- Deciding --------------------------------------------------------------

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approve: bool,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> ApprovalDecision:
        """Record a decision and carry whatever was waiting forward.

        The caller must already have been authorised - the endpoint requires the
        admin role, and that is the security boundary. Nothing here consults a
        button, a flag sent by a browser, or anything else the client controls.

        Raises:
            ApprovalNotFoundError: Not this organization's approval.
            ApprovalAlreadyDecidedError: It was already decided.
            ApprovalNotResumableError: The decision stands; the run cannot go on.
        """
        approval = await self.get(approval_id)

        decision = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        won = await self._approvals.decide(approval.id, decision=decision, decided_by=self._user_id)
        if not won:
            raise ApprovalAlreadyDecidedError()

        # Committed before anything is executed. A decision that is recorded and
        # then lost would be the one failure mode nobody could reconstruct.
        await self._session.commit()

        decided = await self.get(approval.id)
        await record_approval_decision(self._session, decided, decided_by=self._user_id)
        await self._session.commit()

        logger.info(
            "Approval decided",
            extra={
                "context": {
                    "approval_id": str(decided.id),
                    "organization_id": str(self._organization_id),
                    "decision": decision.value,
                    "tool": decided.tool_name,
                    "run_id": str(decided.run_id) if decided.run_id else None,
                    "workflow_run_id": (
                        str(decided.workflow_run_id) if decided.workflow_run_id else None
                    ),
                }
            },
        )

        agent_run: RunView | None = None
        workflow_run: WorkflowRunView | None = None

        # Order matters for an agent step inside a workflow: the agent run has
        # to finish before the workflow can read what it said.
        if decided.run_id is not None:
            agent_run = await self._resume(decided, approve=approve, cancellation=cancellation)

        if decided.workflow_run_id is not None:
            workflow_run = await self._resume_workflow(decided, approve=approve)

        if agent_run is None and workflow_run is None:
            # Nothing to continue: an approval with no process attached, which
            # only a workflow approval row written by something else could be.
            raise ApprovalNotResumableError()

        return ApprovalDecision(approval=decided, agent_run=agent_run, workflow_run=workflow_run)

    async def _resume_workflow(self, approval: Approval, *, approve: bool) -> WorkflowRunView:
        """Carry the paused workflow forward.

        The workflow service owns the engine and the claims; this only routes a
        decision to it. A decision recorded for a run that has since moved on is
        a conflict rather than a silent no-op - the approval stands either way,
        which is the property that matters.
        """
        if self._workflows is None:
            raise ApprovalNotResumableError()

        try:
            return await self._workflows.resume_from_approval(approval, approved=approve)
        except WorkflowRunNotFoundError as exc:
            raise ApprovalNotResumableError() from exc

    # -- Resuming --------------------------------------------------------------

    async def _resume(
        self,
        approval: Approval,
        *,
        approve: bool,
        cancellation: CancellationToken,
    ) -> RunView:
        if approval.run_id is None or approval.tool_execution_id is None:
            # A workflow approval, which this service does not drive.
            raise ApprovalNotResumableError()

        record = await self._runs.get(approval.run_id)
        if record is None or record.status is not AgentRunStatus.AWAITING_APPROVAL:
            raise ApprovalNotResumableError()

        if record.conversation_id is None:
            raise ApprovalNotResumableError()

        rows = await self._conversations.messages(record.conversation_id)
        payload = pending_tool_request(rows, tool_execution_id=approval.tool_execution_id)
        if payload is None:
            raise ApprovalNotResumableError()

        request = pending_request_from_payload(payload)
        history = to_llm_messages(rows)

        if approve and not await self._executions.claim(approval.tool_execution_id):
            # Something already ran this execution. Refusing is the only safe
            # answer: the framework cannot tell whether the side effect happened
            # once or is about to happen twice, and for a destructive tool those
            # are not equally bad.
            raise ApprovalAlreadyDecidedError()

        await self._session.commit()

        result = await self._executor.execute(
            ToolRequest(tool_name=request.tool_name, arguments=request.arguments),
            organization_id=self._organization_id,
            user_id=record.user_id,
            run_id=record.id,
            agent_id=record.agent_id,
            request_id=record.request_id,
            cancellation=cancellation,
            approval=ApprovalGrant(
                tool_execution_id=approval.tool_execution_id,
                granted=approve,
                decided_by=self._user_id,
            ),
        )

        run = self._rebuild(record, pending_tool_execution_id=approval.tool_execution_id)

        journal = DatabaseRunJournal(
            self._session,
            organization_id=self._organization_id,
            record=record,
            conversation_id=record.conversation_id,
            registry=self._tools,
        )

        await journal.record_tool(
            run,
            ToolAttempt(
                # The step that asked. The execution row already exists under
                # this pairing, so the update lands on it rather than creating a
                # second one.
                step_number=max(record.step_count, 1),
                tool_name=request.tool_name,
                argument_count=len(request.arguments),
                executed=reached_the_tool(result),
                result=result,
            ),
        )

        await self._drive(run, history, request, result, journal, cancellation)

        return await self._view(record)

    def _rebuild(self, record: AgentRunRecord, *, pending_tool_execution_id: uuid.UUID) -> AgentRun:
        """The working copy of a paused run, from its durable record.

        Everything the earlier request spent comes back as ``carried``, so the
        step budget continues rather than restarting. The steps themselves are
        not rebuilt: they are already durable, and re-materialising them would
        mean inventing decision objects for rows that deliberately do not store
        one.
        """
        return AgentRun(
            id=record.id,
            agent_id=record.agent_id,
            organization_id=record.organization_id,
            user_id=record.user_id,
            status=AgentRunStatus.AWAITING_APPROVAL,
            created_at=record.created_at,
            started_at=record.started_at,
            request_id=record.request_id,
            carried=build_carryover(record),
            pending_tool_execution_id=pending_tool_execution_id,
        )

    async def _drive(
        self,
        run: AgentRun,
        history: Sequence[LLMMessage],
        request: ToolRequestDecision,
        result: ToolResult,
        journal: DatabaseRunJournal,
        cancellation: CancellationToken,
    ) -> None:
        """Continue the run, and record where it got to either way."""
        try:
            await self._runtime().resume(
                run,
                history,
                request=request,
                result=result,
                journal=journal,
                cancellation=cancellation,
            )
        except (AgentCancelledError, AgentError, LLMError):
            await record_agent_run(self._session, run)
            raise

        await record_agent_run(self._session, run)

    async def _view(self, record: AgentRunRecord) -> RunView:
        """The run as a client sees it, projected by the execution service."""
        execution = AgentExecutionService(
            self._session, self._runtime(), self._settings, self._membership, self._tools
        )
        return await execution.get(record.id)
