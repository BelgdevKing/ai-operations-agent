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

The same shape settles three other questions the human-in-the-loop phase asked,
and it is the same shape on purpose - ``expire_due``, ``cancel_for_run`` and
``cancel_for_workflow_run`` all move rows out of ``pending`` with ``pending`` in
the ``WHERE``. So "the administrator approved it at the moment it expired" and
"somebody cancelled the run while somebody else was approving it" are not
special cases with their own reasoning. They are the same race, decided the same
way, by the same lock on the same row.

Everything here is tenant-scoped, and the composite foreign keys on the table
mean an approval could not have been attached to another organization's run or
execution in the first place.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Row, func, or_, tuple_, update

from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.repositories.tenant import TenantScopedRepository

# The decisions a person can make. Anything else is not a decision and must not
# reach the conditional update.
DECIDABLE = frozenset({ApprovalStatus.APPROVED, ApprovalStatus.REJECTED})

MAX_QUEUE_PAGE = 100
"""Most approvals one request may read. A queue is paged, never dumped."""


class ApprovalRepository(TenantScopedRepository[Approval]):
    """Approvals of one organization."""

    model = Approval

    async def list_pending(self, *, limit: int = 50) -> Sequence[Approval]:
        """The queue: what is waiting on somebody, oldest first."""
        return await self.list_queue(statuses=(ApprovalStatus.PENDING,), limit=limit)

    async def list_queue(
        self,
        *,
        statuses: Sequence[ApprovalStatus] = (),
        tool_name: str | None = None,
        limit: int = 50,
        after: Approval | None = None,
    ) -> Sequence[Approval]:
        """One page of the queue, oldest first.

        Oldest first because an approval queue is a to-do list, and the thing
        that has been waiting longest is the thing most likely to be blocking
        somebody.

        The order is ``(requested_at, id)`` and the paging is keyset rather than
        ``OFFSET``: an offset shifts under you every time a new approval is
        requested, which on a queue - where rows arrive at the *front* of the
        order people read it in - means skipping items that were never shown.
        Paging from the last row seen cannot skip anything, because the row it
        pages from is one the caller has already got.

        Args:
            statuses: Which states to include. Empty means every state.
            tool_name: Restrict to one tool. Matched exactly; a tool name is an
                identifier, so there is nothing here to pattern-match with.
            limit: Bounded by ``MAX_QUEUE_PAGE`` whatever is asked for.
            after: The last row of the previous page.
        """
        statement = self.select().order_by(Approval.requested_at, Approval.id)

        if statuses:
            statement = statement.where(Approval.status.in_(tuple(statuses)))

        if tool_name is not None:
            statement = statement.where(Approval.tool_name == tool_name)

        if after is not None:
            # A row comparison rather than "requested_at > x OR (= x AND id > y)":
            # it is the same condition, it is what the index on the order can
            # actually be used for, and it cannot be got subtly wrong.
            statement = statement.where(
                tuple_(Approval.requested_at, Approval.id) > (after.requested_at, after.id)
            )

        result = await self.session.execute(statement.limit(max(1, min(limit, MAX_QUEUE_PAGE))))
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
        reason: str | None = None,
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

        **A lapsed approval is not decidable**, and the deadline is in the same
        ``WHERE`` clause as the state for the same reason: a decision arriving
        at the instant of expiry has to lose or win once, not be checked in
        Python and then written. Losing to the clock is indistinguishable here
        from losing to another decision - both are False - so the caller reads
        the row back to find out which, and the row is authoritative either way.
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
                self._still_open(),
            )
            .values(
                status=decision,
                approved_by=decided_by,
                approved_at=func.now(),
                decision_reason=reason,
            )
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return (result.rowcount or 0) == 1

    # -- Lapsing ---------------------------------------------------------------

    @staticmethod
    def _still_open() -> ColumnElement[bool]:
        """Whether the deadline has not passed.

        ``func.now()`` is the transaction timestamp and is deliberately the one
        used: every row a single sweep examines is judged against a single
        instant, so a long sweep cannot expire an approval that was still open
        when the sweep began.
        """
        return or_(Approval.expires_at.is_(None), Approval.expires_at > func.now())

    async def expire_due(self) -> Sequence[Row[Any]]:
        """Lapse every pending approval of this organization that is past due.

        Returns the rows this call won - id, and the processes each one was
        holding up - so the caller can stop those runs waiting for an answer
        that can no longer come. Rows another caller expired concurrently are
        not returned, because that caller is dealing with them.

        ``RETURNING`` rather than select-then-update: the set that was changed
        and the set that is acted on afterwards have to be the same set, and a
        separate query would be a different one by the time it ran.
        """
        statement = (
            update(Approval)
            .where(
                Approval.organization_id == self.organization_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at.is_not(None),
                Approval.expires_at <= func.now(),
            )
            .values(status=ApprovalStatus.EXPIRED)
            .returning(
                Approval.id,
                Approval.run_id,
                Approval.workflow_run_id,
                Approval.workflow_step_run_id,
                Approval.tool_execution_id,
                Approval.tool_name,
                Approval.requested_by,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        return result.all()

    # -- Cancelling ------------------------------------------------------------

    async def cancel_for_run(self, run_id: uuid.UUID) -> int:
        """Withdraw the pending approvals of one agent run.

        Called when the run itself is cancelled. ``pending`` is in the ``WHERE``
        here too, so a cancellation racing an approval does not overwrite the
        decision - it simply changes no row, and the run-level guard is what
        settles which of the two actually happened.
        """
        return await self._withdraw(Approval.run_id == run_id)

    async def cancel_for_workflow_run(self, workflow_run_id: uuid.UUID) -> int:
        """Withdraw the pending approvals of one workflow run."""
        return await self._withdraw(Approval.workflow_run_id == workflow_run_id)

    async def _withdraw(self, matching: ColumnElement[bool]) -> int:
        statement = (
            update(Approval)
            .where(
                Approval.organization_id == self.organization_id,
                Approval.status == ApprovalStatus.PENDING,
                matching,
            )
            .values(status=ApprovalStatus.CANCELLED)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self.session.execute(statement))
        return result.rowcount or 0
