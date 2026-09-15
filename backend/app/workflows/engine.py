"""The loop that executes one workflow run.

    claim the run
    while there is a step:
        claim the step            (one row, unique per run)
        run it                    (tool / agent / condition / approval)
        write down what happened  (commit)
        -> paused?   stop, with an approval waiting
        -> failed?   stop, with a code
        -> otherwise carry on to whichever step it named

Sequential, durable and deliberately small. The engine **orchestrates**; it does
not execute anything itself. A tool step goes through the existing
``ToolExecutor``, an agent step through the existing agent execution service, an
approval through the existing ``approvals`` table. There is no second runtime,
no second executor, no second approval state machine, and no place in this file
where a provider is named.

Four properties are worth stating, because the shape of the code is built around
them:

**A step cannot execute twice.** Starting one inserts a row, and
``UNIQUE(workflow_run_id, step_key)`` refuses a second. A duplicate request, a
retried HTTP call or a second resume collides rather than repeating the work.

**A run is driven by one request at a time.** Claiming it is a conditional
update with the expected state in the ``WHERE`` clause.

**Nothing is retried.** A tool that changes something must not be re-run on a
guess, and the tool framework already decides what is safe to repeat. A failed
step stops the run and says which step and why.

**No transaction spans a model call, a tool call or a person.** Each step's
outcome is committed before the next begins, which is what lets a run survive
the request that started it.

A business failure is an *outcome*, not an exception: a tool that could not find
a record leaves the run ``failed`` with a code, and the caller gets a 200 and a
run they can read. Infrastructure failures - the provider is down, the database
is gone - travel up as the exceptions they already are.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRunStatus
from app.core.config import Settings
from app.models.agent_run import ToolExecutionRecord
from app.models.approval import Approval
from app.models.enums import ApprovalStatus, RunStatus, StepRunStatus, WorkflowStepType
from app.models.workflow import WorkflowRun, WorkflowStepRun
from app.observability.instruments import Instruments, NullInstruments
from app.observability.tracing import span
from app.repositories.agent_run import ToolExecutionRepository
from app.repositories.approval import ApprovalRepository
from app.repositories.workflow import WorkflowRunRepository, WorkflowStepRunRepository
from app.services.agent_execution import RunView
from app.tools.models import (
    ApprovalGrant,
    ToolMetadata,
    ToolOutcome,
    ToolRequest,
    ToolResult,
)
from app.tools.registry import ToolRegistry
from app.tools.summary import ActionSummary, build_summary
from app.workflows.context import WorkflowContext, encoded_size, evaluate
from app.workflows.definition import (
    AgentStep,
    ApprovalStep,
    ConditionStep,
    ToolCallStep,
    WorkflowDefinition,
    WorkflowStepDefinition,
)
from app.workflows.exceptions import (
    WorkflowOutputTooLargeError,
    WorkflowReferenceError,
    WorkflowStepLimitError,
)

logger = logging.getLogger(__name__)

APPROVAL_REJECTED_CODE = "workflow_approval_rejected"
STEP_FAILED_CODE = "workflow_step_failed"
STEP_LIMIT_CODE = "workflow_step_limit_exceeded"
OUTPUT_TOO_LARGE_CODE = "workflow_output_too_large"
REFERENCE_CODE = "workflow_reference_unresolved"

# What an approver is told about a gated tool, built from the tool's own
# classification. Never from its arguments - an approver needs to know what kind
# of thing is being asked for, and the arguments are the tenant's business data.
TOOL_REASON = (
    "The workflow asked to run {tool}, which is classified {safety} and cannot be "
    "performed without a person agreeing to it."
)


@dataclass(frozen=True)
class StepOutcome:
    """What one step did, and where the run goes next."""

    status: StepRunStatus
    output: object = None
    next_step: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    #: Set when the step is waiting on a person. Carries the execution the
    #: decision would authorise, when there is one.
    approval_reason: str | None = None
    #: What the person is being asked to allow, already reduced to the fields
    #: the tool declared safe to show. Never the arguments themselves.
    approval_summary: ActionSummary | None = None
    tool_execution_id: uuid.UUID | None = None
    tool_name: str | None = None

    @property
    def paused(self) -> bool:
        return self.status is StepRunStatus.AWAITING_APPROVAL

    @property
    def failed(self) -> bool:
        return self.status is StepRunStatus.FAILED


StepHandler = Callable[
    [WorkflowStepDefinition, WorkflowContext, WorkflowStepRun], Awaitable[StepOutcome]
]


class ToolRunner(Protocol):
    """What the engine needs from the tool framework.

    A structural type rather than the concrete ``ToolExecutor``, so the engine
    depends on *running a tool under the framework's rules* rather than on the
    class that happens to do it - and so an engine test needs no registry.
    """

    async def execute(
        self,
        request: ToolRequest,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID | None = ...,
        agent_id: uuid.UUID | None = ...,
        request_id: str | None = ...,
        approval: ApprovalGrant | None = ...,
    ) -> ToolResult: ...


class AgentStepRunner(Protocol):
    """What the engine needs from the agent execution service.

    Deliberately two methods and no more. The engine can ask for an agent run
    and can read one back; it cannot reach the runtime, the gateway or a
    provider, which is what keeps "the workflow does not know which vendor
    answered" a property of the code rather than a convention.
    """

    async def run_for_workflow(
        self, *, agent_id: str, message: str, idempotency_key: str, request_id: str | None
    ) -> RunView: ...

    async def get(self, run_id: uuid.UUID) -> RunView: ...


class WorkflowEngine:
    """Drives one organization's workflow runs.

    Built per request, from a verified membership. Nothing below takes an
    organization from a workflow definition or from a run's input - those are
    data somebody wrote, and identity is not theirs to state.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        request_id: str | None,
        tools: ToolRunner,
        registry: ToolRegistry,
        agents: AgentStepRunner,
        instruments: Instruments | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._organization_id = organization_id
        self._user_id = user_id
        self._request_id = request_id
        self._tools = tools
        self._registry = registry
        self._agents = agents
        self._instruments = instruments or NullInstruments()

        self._runs = WorkflowRunRepository(session, organization_id)
        self._steps = WorkflowStepRunRepository(session, organization_id)
        self._executions = ToolExecutionRepository(session, organization_id)
        self._approvals = ApprovalRepository(session, organization_id)

        # Central dispatch. A fifth step type is a new class, a new handler and
        # one line here - not a new branch in half a dozen places.
        self._handlers: dict[WorkflowStepType, StepHandler] = {
            WorkflowStepType.TOOL_CALL: self._run_tool,  # type: ignore[dict-item]
            WorkflowStepType.AGENT_STEP: self._run_agent,  # type: ignore[dict-item]
            WorkflowStepType.CONDITION: self._run_condition,  # type: ignore[dict-item]
            WorkflowStepType.APPROVAL: self._run_approval,  # type: ignore[dict-item]
        }

    # -- Entry points ----------------------------------------------------------

    async def start(self, run: WorkflowRun, definition: WorkflowDefinition) -> WorkflowRun:
        """Drive a freshly created run from its entry step.

        Returns without doing anything if another request already claimed it,
        which is what makes a duplicated start safe rather than merely unlikely.
        """
        if not await self._runs.claim(run.id, expected=(RunStatus.PENDING,)):
            await self._session.rollback()
            logger.info(
                "Workflow run already claimed",
                extra={"context": self._log_context(run)},
            )
            return run

        await self._session.commit()
        # Read back what the claim wrote. The update is deliberately not
        # session-synchronising, so the object in hand still says "pending"
        # until it is refreshed - and it is about to be reported to a client.
        await self._session.refresh(run)

        context = WorkflowContext(run.input_data)
        return await self._drive(run, definition, context, definition.entry)

    async def resume(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        step_run: WorkflowStepRun,
        *,
        approved: bool,
    ) -> WorkflowRun:
        """Carry a paused run forward, now that its step has a decision.

        The **same** run and the **same** step: no successor run, no second row,
        and the step keeps the position it already had. Both claims below are
        conditional updates, so two approvals arriving together resolve to one
        resume and one refusal rather than two.
        """
        if not await self._runs.claim(run.id, expected=(RunStatus.AWAITING_APPROVAL,)):
            await self._session.rollback()
            return run

        if not await self._steps.claim(step_run.id):
            await self._session.rollback()
            return run

        await self._session.commit()
        await self._session.refresh(run)
        await self._session.refresh(step_run)

        outputs = await self._steps.completed_outputs(run.id)
        context = WorkflowContext.rebuild(run.input_data, outputs)

        step = definition.step(step_run.step_key)
        outcome = await self._decide(step, context, step_run, approved=approved)

        await self._finish_step(run, step_run, outcome, context)

        if outcome.paused or outcome.failed:
            return run

        return await self._drive(run, definition, context, outcome.next_step)

    # -- The loop --------------------------------------------------------------

    async def _drive(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        context: WorkflowContext,
        start_at: str | None,
    ) -> WorkflowRun:
        current = start_at

        while current is not None:
            if run.step_count >= self._settings.workflow_max_executed_steps:
                # The graph is validated acyclic, so this should be unreachable.
                # It exists because "should be" is not "is", and an engine that
                # could loop is worse than one that stops and says so.
                await self._fail(
                    run,
                    code=STEP_LIMIT_CODE,
                    message=WorkflowStepLimitError.message,
                )
                return run

            step = definition.step(current)
            position = await self._steps.next_position(run.id)

            step_run = await self._steps.begin(
                run_id=run.id,
                step_key=step.id,
                step_type=WorkflowStepType(step.type),
                position=position,
                input_data=None,
            )
            if step_run is None:
                # Somebody else is executing this step. Leave it to them rather
                # than doing the same work a second time.
                logger.info(
                    "Workflow step already claimed",
                    extra={"context": {**self._log_context(run), "step": step.id}},
                )
                return run

            await self._session.commit()

            # One span per step, so a trace shows which step a workflow spent
            # its time in. The step's *type* is an enum; its key is a name the
            # workflow author chose, and therefore unbounded - the same reason
            # the metric label below is the type rather than the key.
            with span(
                "workflow.step",
                attributes={"workflow.step_type": step_run.step_type.value},
            ) as current_span:
                outcome = await self._execute(step, context, step_run)
                await self._finish_step(run, step_run, outcome, context)
                current_span.set_attribute("workflow.step_status", outcome.status.value)
                if outcome.error_code:
                    current_span.set_attributes(
                        {"error.code": outcome.error_code, "error.layer": "workflow"}
                    )

            if outcome.paused or outcome.failed:
                return run

            current = outcome.next_step

        await self._succeed(run, context)
        return run

    async def _execute(
        self,
        step: WorkflowStepDefinition,
        context: WorkflowContext,
        step_run: WorkflowStepRun,
    ) -> StepOutcome:
        """One step, through whichever existing capability it names.

        A reference that cannot be resolved and a result too large to carry are
        both *the step's* failure rather than the engine's, so they become
        outcomes here instead of escaping - the run records which step, with a
        code, and stops.
        """
        handler = self._handlers[WorkflowStepType(step.type)]

        try:
            return await handler(step, context, step_run)
        except WorkflowReferenceError as exc:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=REFERENCE_CODE,
                error_message=str(exc),
            )
        except WorkflowOutputTooLargeError:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=OUTPUT_TOO_LARGE_CODE,
                error_message=WorkflowOutputTooLargeError.message,
            )

    # -- Step handlers ---------------------------------------------------------

    async def _run_tool(
        self, step: ToolCallStep, context: WorkflowContext, step_run: WorkflowStepRun
    ) -> StepOutcome:
        """Run a registered tool through the existing executor.

        Arguments are interpolated - a value either is a reference and is looked
        up, or is used exactly as written. Identity is passed separately and
        never comes from the definition, so there is no expression a workflow
        author could write that would point a tool at another tenant.
        """
        arguments = context.interpolate(step.arguments)

        result = await self._tools.execute(
            ToolRequest(tool_name=step.tool, arguments=arguments),
            organization_id=self._organization_id,
            user_id=self._user_id,
            run_id=step_run.workflow_run_id,
            request_id=self._request_id,
        )

        await self._record_execution(step_run, step.tool, result, arguments_count=len(arguments))

        if result.outcome is ToolOutcome.APPROVAL_REQUIRED:
            metadata = self._metadata(step.tool)
            safety = metadata.safety.value if metadata else "sensitive"
            # The same allow-list the agent path applies, applied to the
            # interpolated arguments. A workflow author cannot widen it: what a
            # reviewer sees is decided by the tool's own code, not by the
            # document that called it.
            summary = (
                build_summary(metadata.approval_summary, arguments)
                if metadata is not None and metadata.approval_summary is not None
                else None
            )
            return StepOutcome(
                status=StepRunStatus.AWAITING_APPROVAL,
                approval_reason=TOOL_REASON.format(tool=step.tool, safety=safety),
                approval_summary=summary,
                tool_execution_id=result.tool_execution_id,
                tool_name=step.tool,
                next_step=step.next,
            )

        if not result.ok:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=result.failure.code if result.failure else STEP_FAILED_CODE,
                error_message=result.failure.message if result.failure else None,
            )

        return StepOutcome(
            status=StepRunStatus.SUCCEEDED,
            output=self._bounded(result.data),
            next_step=step.next,
        )

    async def _run_agent(
        self, step: AgentStep, context: WorkflowContext, step_run: WorkflowStepRun
    ) -> StepOutcome:
        """Run a registered agent through the existing agent execution service.

        The workflow names an agent and a message. It does not know, and cannot
        express, which provider serves the call - that is the gateway's business
        and the agent's configuration.

        The agent run is made idempotent on this step's own id, so a workflow
        step that is somehow attempted twice reaches the same agent run rather
        than paying for a second one.
        """
        references = step.references
        message = context.resolve(references[0]) if references else step.message

        view = await self._agents.run_for_workflow(
            agent_id=step.agent_id,
            message=str(message),
            idempotency_key=f"workflow-step-{step_run.id}",
            request_id=self._request_id,
        )

        record = view.record
        step_run.agent_run_id = record.id

        if record.status is AgentRunStatus.AWAITING_APPROVAL:
            # The agent paused for its own tool approval. The workflow pauses
            # with it, and the approval it already created is stamped with this
            # step so deciding it resumes both.
            approval = view.pending_approval
            if approval is not None:
                approval.workflow_run_id = step_run.workflow_run_id
                approval.workflow_step_run_id = step_run.id
            return StepOutcome(
                status=StepRunStatus.AWAITING_APPROVAL,
                approval_reason=None,
                next_step=step.next,
            )

        if record.status is not AgentRunStatus.COMPLETED:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=record.error_code or STEP_FAILED_CODE,
                error_message=record.error_message,
            )

        return StepOutcome(
            status=StepRunStatus.SUCCEEDED,
            output=self._bounded(
                {
                    "agent_run_id": str(record.id),
                    "response": view.final_response,
                    "tools": [execution.tool_name for execution in view.tool_executions],
                }
            ),
            next_step=step.next,
        )

    async def _run_condition(
        self, step: ConditionStep, context: WorkflowContext, step_run: WorkflowStepRun
    ) -> StepOutcome:
        """Branch on one declarative comparison.

        Both outcomes are explicit in the definition, so which way the run went
        is a recorded fact rather than something a reader has to re-derive.
        """
        del step_run

        result = evaluate(step.condition, context)

        return StepOutcome(
            status=StepRunStatus.SUCCEEDED,
            output={"result": result},
            next_step=step.on_true if result else step.on_false,
        )

    async def _run_approval(
        self, step: ApprovalStep, context: WorkflowContext, step_run: WorkflowStepRun
    ) -> StepOutcome:
        """Stop and ask a person.

        No tool execution is attached: this step gates a decision rather than a
        specific call. A tool that needs approval is gated at its own step,
        where the approval can name the exact execution it authorises.
        """
        del context, step_run

        return StepOutcome(
            status=StepRunStatus.AWAITING_APPROVAL,
            approval_reason=step.reason,
            # Literal text from the definition, with no fields behind it: there
            # is no execution here to summarise, only a question somebody wrote.
            approval_summary=(
                ActionSummary(headline=step.summary) if step.summary is not None else None
            ),
            next_step=step.next,
        )

    # -- Resuming a paused step ------------------------------------------------

    async def _decide(
        self,
        step: WorkflowStepDefinition,
        context: WorkflowContext,
        step_run: WorkflowStepRun,
        *,
        approved: bool,
    ) -> StepOutcome:
        """Finish the step a person was asked about."""
        if isinstance(step, ToolCallStep):
            return await self._resume_tool(step, context, step_run, approved=approved)

        if isinstance(step, AgentStep):
            return await self._resume_agent(step, step_run, approved=approved)

        if isinstance(step, ApprovalStep):
            if approved:
                return StepOutcome(
                    status=StepRunStatus.SUCCEEDED, output={"approved": True}, next_step=step.next
                )
            return self._rejected(step.on_reject, output={"approved": False})

        # A condition never pauses, so a paused one is a bug rather than a
        # state to recover from.
        return StepOutcome(
            status=StepRunStatus.FAILED,
            error_code=STEP_FAILED_CODE,
            error_message="That step cannot be resumed.",
        )

    async def _resume_tool(
        self,
        step: ToolCallStep,
        context: WorkflowContext,
        step_run: WorkflowStepRun,
        *,
        approved: bool,
    ) -> StepOutcome:
        """Run - or decline to run - the tool a person decided about.

        The execution keeps the identity it was refused under, and is claimed
        before it runs. Both are the durable-execution phase's guarantees,
        reused rather than restated: the tool body is reached at most once for
        that execution id, whatever happens above.
        """
        execution = await self._execution_for(step_run)
        if execution is None:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=STEP_FAILED_CODE,
                error_message="The approved execution is no longer available.",
            )

        if approved and not await self._executions.claim(execution.id):
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=STEP_FAILED_CODE,
                error_message="That execution has already been run.",
            )

        await self._session.commit()

        arguments = context.interpolate(step.arguments)
        result = await self._tools.execute(
            ToolRequest(tool_name=step.tool, arguments=arguments),
            organization_id=self._organization_id,
            user_id=self._user_id,
            run_id=step_run.workflow_run_id,
            request_id=self._request_id,
            approval=ApprovalGrant(
                tool_execution_id=execution.id, granted=approved, decided_by=self._user_id
            ),
        )

        await self._record_execution(step_run, step.tool, result, arguments_count=len(arguments))

        if result.outcome is ToolOutcome.REJECTED:
            return self._rejected(None, output={"approved": False})

        if not result.ok:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=result.failure.code if result.failure else STEP_FAILED_CODE,
                error_message=result.failure.message if result.failure else None,
            )

        return StepOutcome(
            status=StepRunStatus.SUCCEEDED,
            output=self._bounded(result.data),
            next_step=step.next,
        )

    async def _resume_agent(
        self, step: AgentStep, step_run: WorkflowStepRun, *, approved: bool
    ) -> StepOutcome:
        """Read where the agent run got to, now that its approval was decided.

        The agent run is resumed by the approval service - it owns that path -
        so by the time this is reached the run has already moved on, and the
        workflow only has to record what it became.
        """
        del approved

        if step_run.agent_run_id is None:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=STEP_FAILED_CODE,
                error_message="The agent run for that step is no longer available.",
            )

        view = await self._agents.get(step_run.agent_run_id)
        record = view.record

        if record.status is AgentRunStatus.AWAITING_APPROVAL:
            return StepOutcome(status=StepRunStatus.AWAITING_APPROVAL, next_step=step.next)

        if record.status is not AgentRunStatus.COMPLETED:
            return StepOutcome(
                status=StepRunStatus.FAILED,
                error_code=record.error_code or STEP_FAILED_CODE,
                error_message=record.error_message,
            )

        return StepOutcome(
            status=StepRunStatus.SUCCEEDED,
            output=self._bounded(
                {
                    "agent_run_id": str(record.id),
                    "response": view.final_response,
                    "tools": [execution.tool_name for execution in view.tool_executions],
                }
            ),
            next_step=step.next,
        )

    def _rejected(self, on_reject: str | None, *, output: object) -> StepOutcome:
        """What a refusal means.

        A path if the definition declared one, and a stop if it did not. Not a
        failure of the platform either way - a person decided, which is the
        whole point of asking - but a process nobody designed a "no" path for
        should halt rather than quietly carry on.
        """
        if on_reject is not None:
            return StepOutcome(status=StepRunStatus.SUCCEEDED, output=output, next_step=on_reject)

        return StepOutcome(
            status=StepRunStatus.FAILED,
            output=output,
            error_code=APPROVAL_REJECTED_CODE,
            error_message="A person declined this action, and the workflow had no path for it.",
        )

    # -- Writing it down -------------------------------------------------------

    async def _finish_step(
        self,
        run: WorkflowRun,
        step_run: WorkflowStepRun,
        outcome: StepOutcome,
        context: WorkflowContext,
    ) -> None:
        """Record the step's outcome and move the run with it. One commit."""
        step_run.status = outcome.status
        step_run.output_data = _as_document(outcome.output)
        step_run.error_code = outcome.error_code
        step_run.error_message = outcome.error_message

        if not outcome.paused:
            step_run.completed_at = datetime.now(UTC)

        run.current_step = step_run.step_key
        run.step_count += 1
        run.updated_at = datetime.now(UTC)

        if outcome.paused:
            run.status = RunStatus.AWAITING_APPROVAL
            await self._request_approval(run, step_run, outcome)
        elif outcome.failed:
            run.status = RunStatus.FAILED
            run.error_code = outcome.error_code
            run.error_message = outcome.error_message
            run.completed_at = datetime.now(UTC)
        else:
            context.record(step_run.step_key, outcome.output)

        await self._session.commit()

        # The step's kind and state, both enums. Never the step *key*, which is
        # a name a workflow author chose and therefore unbounded.
        self._instruments.record_workflow_step(
            step_type=step_run.step_type.value, status=outcome.status.value
        )

        logger.info(
            "Workflow step finished",
            extra={
                "context": {
                    **self._log_context(run),
                    "step": step_run.step_key,
                    "step_type": step_run.step_type.value,
                    "position": step_run.position,
                    "status": outcome.status.value,
                    "error_code": outcome.error_code,
                }
            },
        )

    async def _request_approval(
        self, run: WorkflowRun, step_run: WorkflowStepRun, outcome: StepOutcome
    ) -> None:
        """Put the step in front of a person.

        Nothing is written for an agent step: the agent runtime already created
        an approval for its own tool, and that row was stamped with this step
        rather than duplicated.

        ``parameters`` is left at its empty default, as it is for every agent
        approval. Tool arguments are business data; an approval authorises an
        *execution* by its id, so copying values into a second operational
        record would disclose them to every reader of the queue for no benefit.
        """
        if outcome.approval_reason is None:
            return

        summary = outcome.approval_summary

        self._session.add(
            Approval(
                organization_id=self._organization_id,
                requested_by=run.user_id,
                workflow_run_id=run.id,
                workflow_step_run_id=step_run.id,
                tool_execution_id=outcome.tool_execution_id,
                tool_name=outcome.tool_name,
                action=outcome.tool_name or step_run.step_key,
                reason=outcome.approval_reason,
                summary=summary.headline if summary else None,
                summary_fields=summary.as_dicts() if summary else [],
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.approval_expiration_seconds),
                status=ApprovalStatus.PENDING,
            )
        )

    async def _record_execution(
        self,
        step_run: WorkflowStepRun,
        tool_name: str,
        result: ToolResult,
        *,
        arguments_count: int,
    ) -> None:
        """Keep the platform's record of every tool it has run.

        Metadata only - counts, codes and timings. The arguments and the
        returned records are the tenant's business data; they belong to the
        workflow's own context, which is ``workflow_step_runs.output_data``.
        """
        metadata = self._metadata(tool_name)
        existing = await self._executions.get(result.tool_execution_id)

        if existing is None:
            existing = ToolExecutionRecord(
                id=result.tool_execution_id,
                organization_id=self._organization_id,
                run_id=None,
                workflow_step_run_id=step_run.id,
                step_number=1,
                tool_name=tool_name,
                safety=metadata.safety.value if metadata else None,
                requires_approval=bool(metadata.requires_approval) if metadata else False,
                argument_count=arguments_count,
            )
            self._session.add(existing)

        # Raised, never lowered: the claim that set this flag is the
        # at-most-once guarantee, and a later write must not undo one.
        if result.outcome in {ToolOutcome.SUCCEEDED, ToolOutcome.FAILED, ToolOutcome.TIMED_OUT}:
            existing.executed = True

        existing.outcome = result.outcome.value
        existing.error_code = result.failure.code if result.failure else None
        existing.result_field_count = len(result.data) if result.data else None
        existing.duration_ms = round(result.duration_ms)
        existing.completed_at = datetime.now(UTC)

    async def _execution_for(self, step_run: WorkflowStepRun) -> ToolExecutionRecord | None:
        statement = self._executions.select().where(
            ToolExecutionRecord.workflow_step_run_id == step_run.id
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _succeed(self, run: WorkflowRun, context: WorkflowContext) -> None:
        """Finish the run, carrying the last step's result as its output."""
        run.status = RunStatus.SUCCEEDED
        run.completed_at = datetime.now(UTC)
        run.updated_at = datetime.now(UTC)
        run.output_data = {
            "step": run.current_step,
            "output": _as_document(context.output_of(run.current_step or "")),
        }

        await self._session.commit()

        logger.info("Workflow run finished", extra={"context": self._log_context(run)})

    async def _fail(self, run: WorkflowRun, *, code: str, message: str) -> None:
        run.status = RunStatus.FAILED
        run.error_code = code
        run.error_message = message
        run.completed_at = datetime.now(UTC)
        run.updated_at = datetime.now(UTC)

        await self._session.commit()

        logger.warning(
            "Workflow run failed",
            extra={"context": {**self._log_context(run), "error_code": code}},
        )

    # -- Helpers ---------------------------------------------------------------

    def _metadata(self, tool_name: str) -> ToolMetadata | None:
        """What the registry says about a tool, or nothing if it has none.

        Tolerant on purpose: a step that named a tool which has since been
        removed still produces a durable record of having tried.
        """
        if not self._registry.has(tool_name):
            return None
        return self._registry.resolve(tool_name).metadata

    def _bounded(self, output: object) -> object:
        """Refuse a result the workflow may not carry.

        Refused rather than truncated: a silently shortened result is one a
        later condition would branch on without anybody knowing it was
        incomplete.
        """
        if encoded_size(output) > self._settings.workflow_max_output_bytes:
            raise WorkflowOutputTooLargeError()
        return output

    def _log_context(self, run: WorkflowRun) -> dict[str, Any]:
        """Identifiers, counts and codes. Never input, output or arguments."""
        return {
            "request_id": self._request_id,
            "workflow_run_id": str(run.id),
            "workflow_id": str(run.workflow_id),
            "workflow_version": run.workflow_version,
            "organization_id": str(self._organization_id),
            "status": run.status.value,
            "step_count": run.step_count,
        }


def _as_document(output: object) -> dict[str, Any] | None:
    """Store a step's result as a JSON object.

    A tool returns an object and a condition returns a flag; wrapping the
    non-object case keeps the column's shape honest rather than relying on
    JSONB accepting a bare scalar.
    """
    if output is None:
        return None
    if isinstance(output, dict):
        return output
    return {"value": output}
