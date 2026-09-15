"""Workflow definitions and the runs of them, for one verified membership.

    endpoint -> WorkflowService -> WorkflowEngine -> ToolExecutor / AgentRuntime
                     |                   |
                     v                   v
                PostgreSQL          PostgreSQL

The service owns everything the engine deliberately does not: which tenant, what
a definition has to satisfy before anybody can start it, whether this work has
already been done, and what a client is allowed to see. The engine owns the
loop.

Three things here are worth reading before changing anything.

**Validation happens at activation.** A definition is written as a draft,
checked once against the tools and agents that actually exist, and only then
becomes runnable. Checking on every start would repeat the work and - worse -
would let something that passed yesterday be started today.

**Input is untrusted.** It is somebody's JSON, bounded for size and depth, and
refused outright if it tries to name the identity fields the server establishes.
A workflow cannot state which organization it acts for.

**The durable run is authoritative.** Every response is projected from the row
and its steps, so a replayed request and a fresh one answer the same way.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from pydantic_core import ErrorDetails
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import AgentRegistry
from app.core.config import Settings
from app.models.approval import Approval
from app.models.enums import ApprovalStatus, RunStatus, WorkflowStatus
from app.models.organization import OrganizationMember
from app.models.workflow import Workflow, WorkflowRun, WorkflowStepRun
from app.repositories.approval import ApprovalRepository
from app.repositories.workflow import (
    WorkflowRepository,
    WorkflowRunRepository,
    WorkflowStepRunRepository,
)
from app.services.audit import (
    record_approval_cancelled,
    record_approval_requested,
    record_workflow_run_created,
    record_workflow_run_outcome,
)
from app.tools.models import RESERVED_ARGUMENT_NAMES
from app.tools.registry import ToolRegistry
from app.workflows.context import depth_of, encoded_size
from app.workflows.definition import WorkflowDefinition
from app.workflows.engine import AgentStepRunner, ToolRunner, WorkflowEngine
from app.workflows.exceptions import (
    WorkflowInputError,
    WorkflowNotActiveError,
    WorkflowNotFoundError,
    WorkflowRunNotCancellableError,
    WorkflowRunNotFoundError,
    WorkflowValidationError,
)
from app.workflows.validation import WorkflowLimits, validate_definition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowRunView:
    """One run as a client is allowed to see it, read from the durable record.

    Assembled rather than returned raw, so adding a column cannot accidentally
    publish it.
    """

    record: WorkflowRun
    steps: Sequence[WorkflowStepRun]
    pending_approval: Approval | None
    replayed: bool = False


class WorkflowService:
    """Manages and runs workflows for one organization."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        membership: OrganizationMember,
        *,
        tools: ToolRunner,
        registry: ToolRegistry,
        agents: AgentStepRunner,
        agent_registry: AgentRegistry,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tools = tools
        self._registry = registry
        self._agents = agents
        self._agent_registry = agent_registry

        # Fixed from the caller's verified membership, once, here. Nothing below
        # takes an organization from an argument, a definition or a run's input.
        self._organization_id = membership.organization_id
        self._user_id = membership.user_id

        self._workflows = WorkflowRepository(session, self._organization_id)
        self._runs = WorkflowRunRepository(session, self._organization_id)
        self._steps = WorkflowStepRunRepository(session, self._organization_id)
        self._approvals = ApprovalRepository(session, self._organization_id)

    # -- Definitions -----------------------------------------------------------

    async def create(
        self, *, name: str, description: str | None, definition: dict[str, Any]
    ) -> Workflow:
        """Store a new draft version of *name*.

        Parsed here so a malformed document is refused with the reason, rather
        than stored and discovered at activation. Never active on creation:
        activating is a separate, deliberate act.
        """
        self._parse(definition)

        version = await self._workflows.next_version(name)
        record = await self._workflows.create(
            name=name, description=description, definition=definition, version=version
        )
        await self._session.commit()

        logger.info(
            "Workflow version created",
            extra={
                "context": {
                    "workflow_id": str(record.id),
                    "organization_id": str(self._organization_id),
                    "version": record.version,
                }
            },
        )
        return record

    async def activate(self, workflow_id: uuid.UUID) -> Workflow:
        """Validate a version and make it the runnable one.

        Everything that needs to know about the world happens here: whether the
        tools it calls are installed, whether the agents it names are ones this
        organization may run, whether the graph is a graph. A version that
        cannot pass is not stored as active, so nothing can start it.
        """
        record = await self._require_workflow(workflow_id)

        definition = self._parse(record.definition)
        validate_definition(
            definition,
            limits=WorkflowLimits.from_settings(self._settings),
            tools=self._registry,
            agents=self._agent_registry,
            organization_id=self._organization_id,
        )

        record.status = WorkflowStatus.ACTIVE
        # Activating a version says which edition is current, so the previous
        # one stops being runnable in the same breath. Runs already under way
        # are untouched - they hold their own version.
        await self._workflows.deactivate_other_versions(name=record.name, keep=record.id)
        await self._session.commit()
        # Read back explicitly: the bulk update above synchronises the session,
        # and the caller is about to project every column of this row. Letting
        # that happen through a lazy refresh would be IO from wherever the
        # serialiser happens to run.
        await self._session.refresh(record)

        logger.info(
            "Workflow version activated",
            extra={
                "context": {
                    "workflow_id": str(record.id),
                    "organization_id": str(self._organization_id),
                    "version": record.version,
                }
            },
        )
        return record

    async def deactivate(self, workflow_id: uuid.UUID) -> Workflow:
        """Stop a version being startable. Runs under way are unaffected."""
        record = await self._require_workflow(workflow_id)
        record.status = WorkflowStatus.INACTIVE
        await self._session.commit()
        await self._session.refresh(record)
        return record

    async def list_definitions(self, *, limit: int = 50) -> Sequence[Workflow]:
        return await self._workflows.list_definitions(limit=limit)

    async def get_definition(self, workflow_id: uuid.UUID) -> Workflow:
        return await self._require_workflow(workflow_id)

    # -- Starting a run --------------------------------------------------------

    async def start(
        self,
        workflow_id: uuid.UUID,
        *,
        input_data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> WorkflowRunView:
        """Create a durable run and drive it until it ends or pauses.

        Raises:
            WorkflowNotFoundError: Not this organization's workflow.
            WorkflowNotActiveError: The version is a draft or was retired.
            WorkflowInputError: The input is too large, too deep, or names a
                field that is the server's to decide.
        """
        record = await self._require_workflow(workflow_id)

        if record.status is not WorkflowStatus.ACTIVE:
            raise WorkflowNotActiveError()

        payload = self._checked_input(input_data)
        definition = self._parse(record.definition)

        await self._sweep_abandoned()

        run, created = await self._runs.create_or_claim(
            run_id=uuid.uuid4(),
            workflow=record,
            user_id=self._user_id,
            input_data=payload,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

        if not created:
            # Somebody already did this work, or is doing it now. The honest
            # answer is what that run is, not a second run doing it again.
            logger.info(
                "Workflow run replayed from idempotency key",
                extra={
                    "context": {
                        "workflow_run_id": str(run.id),
                        "organization_id": str(self._organization_id),
                        "status": run.status.value,
                    }
                },
            )
            await record_workflow_run_created(self._session, run, idempotent_replay=True)
            return await self._view(run, replayed=True)

        await record_workflow_run_created(self._session, run)
        await self._session.commit()

        engine = self._engine(request_id)
        await engine.start(run, definition)

        await self._audit_outcome(run)
        return await self._view(run)

    # -- Resuming after a decision --------------------------------------------

    async def resume_from_approval(self, approval: Approval, *, approved: bool) -> WorkflowRunView:
        """Carry a paused workflow forward, now that somebody has decided.

        Called by the approval service, which owns the decision itself. This
        only knows how to continue: the run and the step are both claimed by
        conditional update, so two approvals arriving together produce one
        resume rather than two.

        Raises:
            WorkflowRunNotFoundError: The run or its step has gone, or the run
                has already moved on.
        """
        if approval.workflow_run_id is None or approval.workflow_step_run_id is None:
            raise WorkflowRunNotFoundError()

        run = await self._runs.get(approval.workflow_run_id)
        if run is None or run.status is not RunStatus.AWAITING_APPROVAL:
            raise WorkflowRunNotFoundError()

        step_run = await self._steps.get(approval.workflow_step_run_id)
        if step_run is None:
            raise WorkflowRunNotFoundError()

        workflow = await self._workflows.get(run.workflow_id)
        if workflow is None:
            raise WorkflowRunNotFoundError()

        # The definition the run started with, not whichever is current. That is
        # the whole point of recording the version.
        definition = self._parse(workflow.definition)

        engine = self._engine(run.request_id)
        await engine.resume(run, definition, step_run, approved=approved)

        await self._audit_outcome(run)
        return await self._view(run)

    async def cancel_run(self, run_id: uuid.UUID) -> WorkflowRunView:
        """Stop a workflow run that is waiting on somebody.

        The same shape as the agent runtime's cancellation, for the same
        reasons: the run is claimed first by conditional update, so a decision
        arriving at the same moment either finds the run already cancelled - and
        reports that it cannot be resumed, having executed nothing - or wins the
        run, in which case this call is told it is not cancellable.

        Raises:
            WorkflowRunNotFoundError: Not this organization's run.
            WorkflowRunNotCancellableError: It is not waiting for anybody.
        """
        run = await self._runs.get(run_id)
        if run is None:
            raise WorkflowRunNotFoundError()

        if not await self._runs.cancel_paused(run.id):
            raise WorkflowRunNotCancellableError()

        for approval in await self._approvals.list_for_workflow_run(run.id):
            if approval.status is ApprovalStatus.PENDING:
                await record_approval_cancelled(self._session, approval, cancelled_by=self._user_id)

        await self._approvals.cancel_for_workflow_run(run.id)
        await self._steps.cancel_paused(run.id)
        await self._session.commit()

        await self._audit_outcome(run)
        await self._session.commit()

        return await self._view(run)

    # -- Reading runs back -----------------------------------------------------

    async def get_run(self, run_id: uuid.UUID) -> WorkflowRunView:
        run = await self._runs.get(run_id)
        if run is None:
            raise WorkflowRunNotFoundError()
        return await self._view(run)

    async def list_runs(self, *, limit: int = 20) -> list[WorkflowRunView]:
        return [await self._view(run) for run in await self._runs.list_recent(limit=limit)]

    async def steps_of(self, run_id: uuid.UUID) -> Sequence[WorkflowStepRun]:
        run = await self._runs.get(run_id)
        if run is None:
            raise WorkflowRunNotFoundError()
        return await self._steps.list_for_run(run.id)

    async def _view(self, run: WorkflowRun, *, replayed: bool = False) -> WorkflowRunView:
        # Read the row back before projecting it. The sweep and the engine's
        # claims are bulk updates that deliberately do not synchronise the
        # session, so the object in hand can be behind what is stored - and the
        # stored row is the authoritative one.
        await self._session.refresh(run)

        steps = await self._steps.list_for_run(run.id)

        pending: Approval | None = None
        if run.status is RunStatus.AWAITING_APPROVAL:
            pending = next(
                (
                    approval
                    for approval in await self._approvals.list_for_workflow_run(run.id)
                    if approval.status is ApprovalStatus.PENDING
                ),
                None,
            )

        return WorkflowRunView(record=run, steps=steps, pending_approval=pending, replayed=replayed)

    # -- Helpers ---------------------------------------------------------------

    def _engine(self, request_id: str | None) -> WorkflowEngine:
        return WorkflowEngine(
            self._session,
            settings=self._settings,
            organization_id=self._organization_id,
            user_id=self._user_id,
            request_id=request_id,
            tools=self._tools,
            registry=self._registry,
            agents=self._agents,
        )

    async def _require_workflow(self, workflow_id: uuid.UUID) -> Workflow:
        record = await self._workflows.get(workflow_id)
        if record is None:
            # The same answer whether it does not exist or belongs to another
            # tenant, so an id cannot be used to discover another organization's
            # processes.
            raise WorkflowNotFoundError()
        return record

    def _parse(self, document: dict[str, Any]) -> WorkflowDefinition:
        """Turn the stored JSON into the validated document type.

        Every structural rule - unique ids, transitions that land, operators
        that exist, references that parse - is enforced by the model, so a
        document that gets past here cannot contain an unknown step type or an
        expression dressed up as one.
        """
        try:
            return WorkflowDefinition.model_validate(document)
        except PydanticValidationError as exc:
            raise WorkflowValidationError(
                details={"problems": [_readable(error) for error in exc.errors()]}
            ) from exc

    def _checked_input(self, input_data: dict[str, Any] | None) -> dict[str, Any]:
        """Bound and sanitise what a run is started with.

        Workflow input is untrusted. Three rules, and the third is the one that
        matters: a run's identity - which organization, which user, which
        request - is established by the server from a verified membership, and a
        payload that names one of those fields is either confused or trying
        something. Either way it is refused rather than ignored, because
        silently dropping a field somebody sent is how they come to believe it
        worked.
        """
        payload = dict(input_data or {})

        smuggled = {key for key in payload if key.lower() in RESERVED_ARGUMENT_NAMES}
        if smuggled:
            raise WorkflowInputError(
                "Workflow input cannot contain fields that name identity or credentials.",
                details={"rejected_fields": sorted(smuggled)},
            )

        size = encoded_size(payload)
        if size > self._settings.workflow_max_input_bytes:
            raise WorkflowInputError(
                f"The input is {size} bytes; the limit is "
                f"{self._settings.workflow_max_input_bytes}."
            )

        depth = depth_of(payload)
        if depth > self._settings.workflow_max_input_depth:
            raise WorkflowInputError(
                f"The input is nested {depth} levels deep; the limit is "
                f"{self._settings.workflow_max_input_depth}."
            )

        return payload

    async def _sweep_abandoned(self) -> int:
        """Fail this organization's runs that nothing is driving any more.

        Run here rather than on a timer because there is no timer. A single
        indexed UPDATE scoped to one tenant, so the cost of doing it on the way
        into a run is not worth optimising away.
        """
        failed = await self._runs.sweep_abandoned(
            stale_after=timedelta(seconds=self._settings.workflow_run_stale_after_seconds)
        )
        if failed:
            await self._session.commit()
            logger.warning(
                "Abandoned workflow runs failed",
                extra={
                    "context": {
                        "organization_id": str(self._organization_id),
                        "runs": failed,
                        "code": "workflow_run_abandoned",
                    }
                },
            )
        return failed

    async def _audit_outcome(self, run: WorkflowRun) -> None:
        await record_workflow_run_outcome(self._session, run)

        if run.status is RunStatus.AWAITING_APPROVAL:
            pending = next(
                (
                    approval
                    for approval in await self._approvals.list_for_workflow_run(run.id)
                    if approval.status is ApprovalStatus.PENDING
                ),
                None,
            )
            if pending is not None:
                await record_approval_requested(self._session, pending)

        await self._session.commit()


def _readable(error: ErrorDetails) -> str:
    """One Pydantic error as a sentence, naming the field and the rule.

    The location and the message only - never the value, which for a definition
    is somebody's document and for input would be their business data.
    """
    location = ".".join(str(part) for part in error.get("loc", ()))
    return f"{location or 'definition'}: {error.get('msg', 'is invalid')}"
