"""Data access for workflow definitions and their executions.

Three repositories, all tenant-scoped. Three of the methods are concurrency
guarantees rather than queries, and they are the reason the engine can be a
plain sequential loop:

``WorkflowRunRepository.create_or_claim``
    Two requests carrying the same idempotency key contend on a unique index.
    One inserts; the other reads back what already exists.

``WorkflowRunRepository.claim``
    Moves a run into ``running`` with the expected state in the ``WHERE``
    clause, so exactly one request drives a run at a time - whether that is the
    request that started it or the one that resumed it after an approval.

``WorkflowStepRunRepository.begin``
    Inserts the step's row. ``UNIQUE(workflow_run_id, step_key)`` means a second
    attempt at the same step collides instead of doing the work again, which is
    what "a step must not execute twice" actually rests on.

Everything else is an ordinary scoped read.

Every bulk ``UPDATE`` here runs with ``synchronize_session=False``. The default
tries to reconcile the session's loaded objects with what the statement changed,
which expires their attributes - and an expired attribute read later, from
wherever a response happens to be serialised, is database IO in a place that
cannot await. The engine holds the objects it is driving and refreshes them
explicitly after a claim, which is both cheaper and legible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.enums import RunStatus, StepRunStatus, WorkflowStatus, WorkflowStepType
from app.models.workflow import Workflow, WorkflowRun, WorkflowStepRun
from app.repositories.tenant import TenantScopedRepository

CANCELLED_ERROR_CODE = "workflow_run_cancelled"
CANCELLED_ERROR_MESSAGE = (
    "Somebody stopped this run while it was waiting for approval. Nothing further was performed."
)

ABANDONED_ERROR_CODE = "workflow_run_abandoned"
"""Why a run that nothing is driving any more is marked failed."""

ABANDONED_ERROR_MESSAGE = (
    "The workflow run stopped before it finished, and could not be safely resumed."
)

#: States a run is supposed to be making progress in. ``awaiting_approval`` is
#: deliberately absent - it is paused because somebody was asked, and its age
#: says nothing about whether they still intend to answer.
SWEEPABLE = (RunStatus.PENDING, RunStatus.RUNNING)


class WorkflowRepository(TenantScopedRepository[Workflow]):
    """Workflow definitions of one organization."""

    model = Workflow

    async def create(
        self,
        *,
        name: str,
        description: str | None,
        definition: dict[str, Any],
        version: int,
    ) -> Workflow:
        """Add a draft version. Never active on creation - see ``activate``."""
        workflow = Workflow(
            name=name,
            description=description,
            definition=definition,
            version=version,
            status=WorkflowStatus.DRAFT,
        )
        self.add(workflow)
        await self.session.flush()
        return workflow

    async def next_version(self, name: str) -> int:
        """The version number a new edition of *name* should take."""
        statement = (
            select(func.coalesce(func.max(Workflow.version), 0) + 1)
            .select_from(Workflow)
            .where(Workflow.organization_id == self.organization_id, Workflow.name == name)
        )
        result = await self.session.execute(statement)
        return int(result.scalar_one())

    async def list_definitions(self, *, limit: int = 50) -> Sequence[Workflow]:
        """This organization's workflow versions, newest first."""
        statement = self.select().order_by(Workflow.name, Workflow.version.desc()).limit(limit)
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def active_version(self, name: str) -> Workflow | None:
        """The runnable edition of *name*, if there is one."""
        statement = (
            self.select()
            .where(Workflow.name == name, Workflow.status == WorkflowStatus.ACTIVE)
            .order_by(Workflow.version.desc())
            .limit(1)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def deactivate_other_versions(self, *, name: str, keep: uuid.UUID) -> int:
        """Retire every other active edition of *name*.

        Activating a version is a statement about which edition is current, so
        the previous one stops being runnable in the same breath. Runs already
        under way are untouched: they hold their own ``workflow_id`` and
        ``workflow_version``, which is the whole point of versioning.
        """
        statement = (
            update(Workflow)
            .where(
                Workflow.organization_id == self.organization_id,
                Workflow.name == name,
                Workflow.id != keep,
                Workflow.status == WorkflowStatus.ACTIVE,
            )
            .values(status=WorkflowStatus.INACTIVE)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0


class WorkflowRunRepository(TenantScopedRepository[WorkflowRun]):
    """Durable workflow runs of one organization."""

    model = WorkflowRun

    async def create_or_claim(
        self,
        *,
        run_id: uuid.UUID,
        workflow: Workflow,
        user_id: uuid.UUID,
        input_data: dict[str, Any],
        idempotency_key: str | None,
        request_id: str | None,
    ) -> tuple[WorkflowRun, bool]:
        """Insert a run, or find the one that already owns *idempotency_key*.

        Returns the record and whether this call created it. The uniqueness is
        the database's - ``UNIQUE(organization_id, idempotency_key)`` - so two
        application processes contend correctly without sharing anything.

        The insert runs inside a savepoint so a refused one does not poison the
        surrounding transaction, and commits immediately so the loser's blocked
        insert is released as soon as possible.
        """
        if idempotency_key is None:
            record = self._build(run_id, workflow, user_id, input_data, None, request_id)
            self.add(record)
            await self.session.commit()
            return record, True

        try:
            async with self.session.begin_nested():
                record = self._build(
                    run_id, workflow, user_id, input_data, idempotency_key, request_id
                )
                self.add(record)
                await self.session.flush()
        except IntegrityError:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                # A different constraint fired, so this is a real error and must
                # not be reported as a duplicate.
                raise
            return existing, False

        await self.session.commit()
        return record, True

    def _build(
        self,
        run_id: uuid.UUID,
        workflow: Workflow,
        user_id: uuid.UUID,
        input_data: dict[str, Any],
        idempotency_key: str | None,
        request_id: str | None,
    ) -> WorkflowRun:
        return WorkflowRun(
            id=run_id,
            organization_id=self.organization_id,
            workflow_id=workflow.id,
            # Recorded as well as the id: a run follows the edition it started
            # with, and this is the field somebody reads to know which.
            workflow_version=workflow.version,
            user_id=user_id,
            input_data=input_data,
            idempotency_key=idempotency_key,
            request_id=request_id,
            status=RunStatus.PENDING,
        )

    async def get_by_idempotency_key(self, idempotency_key: str) -> WorkflowRun | None:
        statement = self.select().where(WorkflowRun.idempotency_key == idempotency_key)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_recent(self, *, limit: int = 20) -> Sequence[WorkflowRun]:
        statement = (
            self.select().order_by(WorkflowRun.created_at.desc(), WorkflowRun.id).limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def claim(self, run_id: uuid.UUID, *, expected: Sequence[RunStatus]) -> bool:
        """Take the right to drive this run, once.

        A conditional update with the expected state in the ``WHERE`` clause, so
        the database picks the winner. Returns True to exactly one caller: the
        request that starts a run, or the one approval that resumes it.
        """
        statement = (
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == self.organization_id,
                WorkflowRun.status.in_(tuple(expected)),
            )
            .values(
                status=RunStatus.RUNNING,
                started_at=func.coalesce(WorkflowRun.started_at, func.now()),
            )
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1

    async def sweep_abandoned(self, *, stale_after: timedelta) -> int:
        """Fail this organization's runs that nothing is driving any more.

        The same reasoning as the agent runtime's sweep, and deliberately the
        same remedy. There is no background worker: a run is advanced inside the
        request that asked for it, and if that request dies the run is left with
        nothing to continue it. Mid-execution resume is not attempted, because
        the run may have stopped inside a tool and nothing here can tell whether
        that tool's side effect happened.

        ``awaiting_approval`` is never swept.

        Returns how many runs were failed.
        """
        cutoff = datetime.now(UTC) - stale_after

        statement = (
            update(WorkflowRun)
            .where(
                WorkflowRun.organization_id == self.organization_id,
                WorkflowRun.status.in_(SWEEPABLE),
                WorkflowRun.updated_at < cutoff,
            )
            .values(
                status=RunStatus.FAILED,
                error_code=ABANDONED_ERROR_CODE,
                error_message=ABANDONED_ERROR_MESSAGE,
                completed_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0

    async def cancel_paused(self, run_id: uuid.UUID) -> bool:
        """Stop a workflow run that is waiting on a person, if it still is.

        The agent runtime's reasoning, unchanged: ``awaiting_approval`` is the
        only state with nothing in flight, so it is the only one a durable
        cancellation can end without racing the request that is driving it.

        Returns True when this call cancelled the run.
        """
        statement = (
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == self.organization_id,
                WorkflowRun.status == RunStatus.AWAITING_APPROVAL,
            )
            .values(
                status=RunStatus.CANCELLED,
                error_code=CANCELLED_ERROR_CODE,
                error_message=CANCELLED_ERROR_MESSAGE,
                completed_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1


class WorkflowStepRunRepository(TenantScopedRepository[WorkflowStepRun]):
    """Step executions of one organization's workflow runs."""

    model = WorkflowStepRun

    async def begin(
        self,
        *,
        run_id: uuid.UUID,
        step_key: str,
        step_type: WorkflowStepType,
        position: int,
        input_data: dict[str, Any] | None,
    ) -> WorkflowStepRun | None:
        """Start a step, or report that something already has.

        Returns None when the row already exists - a duplicate request, a
        retried HTTP call, a second resume. The caller stops rather than doing
        the work again; the uniqueness constraint is what makes that safe under
        genuine concurrency rather than merely likely.
        """
        record = WorkflowStepRun(
            organization_id=self.organization_id,
            workflow_run_id=run_id,
            step_key=step_key,
            step_type=step_type,
            position=position,
            input_data=input_data,
            status=StepRunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )

        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError:
            return None

        return record

    async def get_by_key(self, run_id: uuid.UUID, step_key: str) -> WorkflowStepRun | None:
        statement = self.select().where(
            WorkflowStepRun.workflow_run_id == run_id, WorkflowStepRun.step_key == step_key
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[WorkflowStepRun]:
        """Every step of a run, in the order it executed."""
        statement = (
            self.select()
            .where(WorkflowStepRun.workflow_run_id == run_id)
            .order_by(WorkflowStepRun.position)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def completed_outputs(self, run_id: uuid.UUID) -> dict[str, Any]:
        """What each finished step produced, for rebuilding the context.

        Only successful steps. A step that failed, was skipped or is waiting on
        a person has no output a later step should be able to read.
        """
        statement = (
            self.select()
            .where(
                WorkflowStepRun.workflow_run_id == run_id,
                WorkflowStepRun.status == StepRunStatus.SUCCEEDED,
            )
            .order_by(WorkflowStepRun.position)
        )
        result = await self.session.execute(statement)
        return {row.step_key: row.output_data for row in result.scalars().all()}

    async def next_position(self, run_id: uuid.UUID) -> int:
        """Where the next step goes in the run's order, from 1."""
        statement = (
            select(func.coalesce(func.max(WorkflowStepRun.position), 0) + 1)
            .select_from(WorkflowStepRun)
            .where(
                WorkflowStepRun.organization_id == self.organization_id,
                WorkflowStepRun.workflow_run_id == run_id,
            )
        )
        result = await self.session.execute(statement)
        return int(result.scalar_one())

    async def cancel_paused(self, run_id: uuid.UUID) -> int:
        """Close the steps of a cancelled run that were waiting on somebody.

        Cosmetic in the sense that nothing reads a step run to decide whether to
        execute - the run's own status settles that - and not cosmetic at all in
        the sense that a cancelled run whose step still says "waiting for
        approval" is a record that contradicts itself.
        """
        statement = (
            update(WorkflowStepRun)
            .where(
                WorkflowStepRun.organization_id == self.organization_id,
                WorkflowStepRun.workflow_run_id == run_id,
                WorkflowStepRun.status == StepRunStatus.AWAITING_APPROVAL,
            )
            .values(status=StepRunStatus.CANCELLED, completed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0

    async def claim(self, step_run_id: uuid.UUID) -> bool:
        """Take the right to continue a paused step, once.

        The step-level half of the resume guarantee: two approvals arriving
        together both try this, and the database lets exactly one through.
        """
        statement = (
            update(WorkflowStepRun)
            .where(
                WorkflowStepRun.id == step_run_id,
                WorkflowStepRun.organization_id == self.organization_id,
                WorkflowStepRun.status == StepRunStatus.AWAITING_APPROVAL,
            )
            .values(status=StepRunStatus.RUNNING)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1
