"""Data access for human approvals.

One method matters more than the rest. ``decide`` moves an approval out of
``pending`` with a single conditional ``UPDATE``:

    UPDATE approvals SET status = :decision ...
     WHERE id = :id AND organization_id = :tenant AND status = 'pending'

The row is not read, checked in Python and written back - that pattern has a
window between the read and the write in which a second request can make the
same decision, and two people approving the same destructive action at the same
moment is exactly the case this exists for. With the state in the ``WHERE``
clause, PostgreSQL serialises the two updates itself: the first changes one row
and the second changes none, so "did I win?" is ``rowcount == 1``.

Everything here is tenant-scoped, and the composite foreign keys on the table
mean an approval could not have been attached to another organization's run or
execution in the first place.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, func, update

from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.repositories.tenant import TenantScopedRepository

# The decisions a person can make. Anything else is not a decision and must not
# reach the conditional update.
DECIDABLE = frozenset({ApprovalStatus.APPROVED, ApprovalStatus.REJECTED})


class ApprovalRepository(TenantScopedRepository[Approval]):
    """Approvals of one organization."""

    model = Approval

    async def list_pending(self, *, limit: int = 50) -> Sequence[Approval]:
        """The queue: what is waiting on somebody, oldest first.

        Oldest first because an approval queue is a to-do list, and the thing
        that has been waiting longest is the thing most likely to be blocking
        somebody.
        """
        statement = (
            self.select()
            .where(Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.requested_at, Approval.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[Approval]:
        """Every approval raised by one run, oldest first."""
        statement = (
            self.select()
            .where(Approval.run_id == run_id)
            .order_by(Approval.requested_at, Approval.id)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def list_for_workflow_run(self, workflow_run_id: uuid.UUID) -> Sequence[Approval]:
        """Every approval raised by one workflow run, oldest first."""
        statement = (
            self.select()
            .where(Approval.workflow_run_id == workflow_run_id)
            .order_by(Approval.requested_at, Approval.id)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_for_execution(self, tool_execution_id: uuid.UUID) -> Approval | None:
        """The approval covering one execution, if there is one."""
        statement = self.select().where(Approval.tool_execution_id == tool_execution_id)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        decision: ApprovalStatus,
        decided_by: uuid.UUID,
    ) -> bool:
        """Record a decision, if this call is the one that gets to make it.

        Returns True when this call moved the approval out of ``pending``, and
        False when it was already decided - by another request, by the same
        person clicking twice, or by an expiry sweep. A False is a conflict for
        the caller to report, never a reason to run anything.

        ``approved_by`` and ``approved_at`` hold the decision whichever way it
        went. The columns predate rejection being representable; adding a second
        pair named ``decided_*`` would mean two places to look for the same
        fact, and the ``status`` column already says which decision it was.
        """
        if decision not in DECIDABLE:
            raise ValueError(f"{decision!r} is not a decision a person can record.")

        statement = (
            update(Approval)
            .where(
                Approval.id == approval_id,
                Approval.organization_id == self.organization_id,
                # The guard. Without it this is a read-then-write race.
                Approval.status == ApprovalStatus.PENDING,
            )
            .values(
                status=decision,
                approved_by=decided_by,
                approved_at=func.now(),
            )
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1
