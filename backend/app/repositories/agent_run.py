"""Data access for durable agent execution.

Three repositories, all tenant-scoped, and three things worth reading closely
because each one is a concurrency guarantee rather than a query:

``AgentRunRepository.create_or_claim``
    Two requests carrying the same idempotency key contend on a unique index,
    not on anything this process remembers. One inserts; the other is told what
    already exists.

``ToolExecutionRepository.claim``
    Marks an execution as run *before* it runs, with a conditional update. Two
    approvals of the same execution cannot both win the claim, so the tool body
    is reached at most once for that identity.

``AgentRunRepository.sweep_abandoned``
    Turns runs whose request died into failures, and leaves paused ones alone.

Everything else here is an ordinary scoped read.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError

from app.agents.models import ACTIVE_STATUSES, AgentRunStatus
from app.models.agent_run import AgentRunRecord, AgentStepRecord, ToolExecutionRecord
from app.repositories.tenant import TenantScopedRepository

ABANDONED_ERROR_CODE = "agent_run_abandoned"
"""Why a run that nobody is driving any more is marked failed."""

ABANDONED_ERROR_MESSAGE = (
    "The agent run stopped before it finished, and could not be safely resumed."
)


class AgentRunRepository(TenantScopedRepository[AgentRunRecord]):
    """Durable runs of one organization."""

    model = AgentRunRecord

    async def create_or_claim(
        self,
        *,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        idempotency_key: str | None,
        request_id: str | None,
    ) -> tuple[AgentRunRecord, bool]:
        """Insert a run, or find the one that already owns *idempotency_key*.

        Returns the record and whether this call created it.

        The uniqueness is the database's: ``UNIQUE(organization_id,
        idempotency_key)``. Two requests racing with the same key both try to
        insert; PostgreSQL makes the second wait on the index and then refuses
        it, and the loser reads back the winner's row instead of raising. No
        dictionary, cache or process-local set is involved, which is the point -
        two application processes would not share one.

        The insert runs inside a savepoint so that a refused one does not poison
        the surrounding transaction, and is committed immediately so that the
        loser's blocked insert is released as soon as possible.
        """
        if idempotency_key is None:
            # Nothing to contend over. A run without a key is simply a new run.
            record = self._build(run_id, user_id, agent_id, None, request_id)
            self.add(record)
            await self.session.commit()
            return record, True

        try:
            async with self.session.begin_nested():
                record = self._build(run_id, user_id, agent_id, idempotency_key, request_id)
                self.add(record)
                await self.session.flush()
        except IntegrityError:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                # The constraint that fired was not the idempotency one, so this
                # is a real error and must not be reported as a duplicate.
                raise
            return existing, False

        await self.session.commit()
        return record, True

    def _build(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        idempotency_key: str | None,
        request_id: str | None,
    ) -> AgentRunRecord:
        return AgentRunRecord(
            id=run_id,
            organization_id=self.organization_id,
            user_id=user_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
            status=AgentRunStatus.PENDING,
        )

    async def get_by_idempotency_key(self, idempotency_key: str) -> AgentRunRecord | None:
        """The run this organization already made under *idempotency_key*."""
        statement = self.select().where(AgentRunRecord.idempotency_key == idempotency_key)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_recent(self, *, limit: int = 20) -> Sequence[AgentRunRecord]:
        """This organization's runs, newest first."""
        statement = (
            self.select().order_by(AgentRunRecord.created_at.desc(), AgentRunRecord.id).limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def sweep_abandoned(self, *, stale_after: timedelta) -> int:
        """Fail this organization's runs that nobody is driving any more.

        There is no background worker in this architecture: a run is advanced
        inside the HTTP request that asked for it. If that request dies - the
        process restarts, the connection drops, the container is replaced - the
        run is left ``pending`` or ``running`` with nothing to continue it.

        Mid-execution resume is deliberately *not* attempted. The run may have
        been inside a model call or inside a tool when it stopped, and nothing
        here can tell whether that tool's side effect happened. Re-running it
        would be the one mistake this system is built to avoid. So the honest
        remedy is to mark the run failed, with a code that says exactly what
        happened, and let a person start again.

        ``awaiting_approval`` is never swept. It is paused on purpose, and its
        age says nothing about whether anyone still cares - only that nobody has
        answered yet. Expiring an approval is a separate decision with its own
        column (``approvals.expires_at``) and is not this sweep's business.

        Returns how many runs were failed.
        """
        cutoff = datetime.now(UTC) - stale_after

        statement = (
            update(AgentRunRecord)
            .where(
                AgentRunRecord.organization_id == self.organization_id,
                AgentRunRecord.status.in_(tuple(ACTIVE_STATUSES)),
                AgentRunRecord.updated_at < cutoff,
            )
            .values(
                status=AgentRunStatus.FAILED,
                error_code=ABANDONED_ERROR_CODE,
                error_message=ABANDONED_ERROR_MESSAGE,
                completed_at=func.now(),
            )
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0


class AgentStepRepository(TenantScopedRepository[AgentStepRecord]):
    """Steps of one organization's runs."""

    model = AgentStepRecord

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[AgentStepRecord]:
        statement = (
            self.select()
            .where(AgentStepRecord.run_id == run_id)
            .order_by(AgentStepRecord.step_number)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()


class ToolExecutionRepository(TenantScopedRepository[ToolExecutionRecord]):
    """Tool executions of one organization's runs."""

    model = ToolExecutionRecord

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[ToolExecutionRecord]:
        statement = (
            self.select()
            .where(ToolExecutionRecord.run_id == run_id)
            .order_by(ToolExecutionRecord.step_number)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def count_executed(self, run_id: uuid.UUID, *, tool_name: str | None = None) -> int:
        """How many of a run's tools actually reached their body.

        The number the approval tests assert on: zero while a decision is
        pending, exactly one after it is granted, and still exactly one however
        many times the approval is repeated.
        """
        statement = (
            select(func.count())
            .select_from(ToolExecutionRecord)
            .where(
                ToolExecutionRecord.organization_id == self.organization_id,
                ToolExecutionRecord.run_id == run_id,
                ToolExecutionRecord.executed.is_(True),
            )
        )
        if tool_name is not None:
            statement = statement.where(ToolExecutionRecord.tool_name == tool_name)
        result = await self.session.execute(statement)
        return result.scalar_one()

    async def claim(self, execution_id: uuid.UUID) -> bool:
        """Take the right to run this execution's tool, once.

        A conditional update - ``SET executed = true WHERE executed = false`` -
        so the database decides the winner. Returns True to exactly one caller
        and False to every other, however many arrive at once.

        **Claimed before the tool runs, not after.** If the process dies between
        the claim and the side effect, the execution stays claimed and the tool
        is never re-run. That is the deliberate trade: for a destructive action,
        "possibly not done" is recoverable by a person and "possibly done twice"
        is not.
        """
        statement = (
            update(ToolExecutionRecord)
            .where(
                ToolExecutionRecord.id == execution_id,
                ToolExecutionRecord.organization_id == self.organization_id,
                ToolExecutionRecord.executed.is_(False),
            )
            .values(executed=True)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1
