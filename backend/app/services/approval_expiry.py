"""Approvals nobody answered, and the runs they were holding up.

An approval that waits forever is not a pause, it is a leak. The run keeps its
conversation, its claimed execution and its place at the top of somebody's
queue; the shipment it proposed to cancel stays in whatever state made the
proposal sensible three weeks ago; and the reviewer who eventually opens it has
no way to tell whether "pending" means "nobody has looked" or "everybody
decided not to". So a pending approval carries a deadline, stamped when it was
requested, and this is what happens when the deadline passes.

**Lapsing is a transition, not a decision.** The approval becomes ``expired``,
which is not ``rejected``: nobody refused it, and the record should not claim
somebody did. The run it paused is failed with ``approval_expired`` rather than
being sent down the workflow's ``on_reject`` path - that path is a business
branch a human "no" was designed to take, and taking it because a clock ran out
would put the platform's silence in a person's mouth. Starting again is a
decision for somebody who can see why it lapsed.

**One instant per sweep.** The expiry is decided by ``expires_at <= now()`` in
the same statement that writes the new status, so an approval either lapses or
is decided, exactly once, whichever request gets the row lock first. A ``RETURNING``
clause hands back precisely the rows this caller won, and only those runs are
stopped - a concurrent sweep is dealing with the others.

**There is no background worker**, by the same architectural decision as the
abandonment sweep in :mod:`app.services.recovery`. Lapsing happens when somebody
reads the queue, when somebody tries to decide something, and when an operator
runs ``scripts/expire_approvals.py``. The consequence is honest and worth
stating: an approval is *decidable* until its deadline and *not decidable*
after it, immediately and in every path, but its row may say ``pending`` for a
while longer if nothing has looked at this organization since. Nothing acts on
that stale row - the decision path checks the clock, not the column.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRunStatus
from app.models.agent_run import AgentRunRecord
from app.models.enums import RunStatus, StepRunStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowRun, WorkflowStepRun
from app.repositories.approval import ApprovalRepository
from app.services.audit import record_approval_expired

logger = logging.getLogger(__name__)

EXPIRED_ERROR_CODE = "approval_expired"
"""Why a run stopped. Distinct from a rejection, which is somebody's answer."""

EXPIRED_ERROR_MESSAGE = (
    "The approval this was waiting on expired before anybody decided it. Nothing was performed."
)


async def expire_due(session: AsyncSession, *, organization_id: uuid.UUID) -> int:
    """Lapse this organization's overdue approvals, and stop what they held up.

    Tenant-scoped through :class:`ApprovalRepository`, so this is safe to call
    from a request: it cannot reach another organization's approvals even by
    mistake, because the repository is the only place the filter is written.

    Returns how many approvals lapsed.
    """
    approvals = ApprovalRepository(session, organization_id)
    lapsed = await approvals.expire_due()

    if not lapsed:
        return 0

    await _fail_agent_runs(session, organization_id, _ids(lapsed, "run_id"))
    await _fail_workflow_runs(session, organization_id, _ids(lapsed, "workflow_run_id"))
    await _fail_step_runs(session, organization_id, _ids(lapsed, "workflow_step_run_id"))

    for row in lapsed:
        await record_approval_expired(
            session,
            organization_id=organization_id,
            approval_id=row.id,
            run_id=row.run_id,
            workflow_run_id=row.workflow_run_id,
            tool_execution_id=row.tool_execution_id,
            tool_name=row.tool_name,
            requested_by=row.requested_by,
        )

    await session.commit()

    logger.info(
        "Approvals expired",
        extra={
            "context": {
                "organization_id": str(organization_id),
                "approvals_expired": len(lapsed),
                "code": EXPIRED_ERROR_CODE,
            }
        },
    )
    return len(lapsed)


async def expire_every_organization(session: AsyncSession) -> int:
    """Lapse overdue approvals across the whole deployment.

    An operator's tool, run from a script with the deployment's own credentials
    and deliberately not reachable from the API. It loops organizations and
    calls the tenant-scoped path for each rather than writing one unscoped
    ``UPDATE``: the loop costs a statement per tenant and buys the guarantee
    that there is exactly one piece of code in this system that expires an
    approval, and it is the one with the tenant filter in it.

    Returns how many approvals lapsed.
    """
    organizations = (await session.execute(select(Organization.id))).scalars().all()

    expired = 0
    for organization_id in organizations:
        expired += await expire_due(session, organization_id=organization_id)

    logger.info(
        "Deployment-wide approval expiry finished",
        extra={
            "context": {
                "organizations": len(organizations),
                "approvals_expired": expired,
            }
        },
    )
    return expired


def _ids(rows: Sequence[Row[Any]], field: str) -> list[uuid.UUID]:
    """The non-null values of one column across the rows that lapsed."""
    return [value for row in rows if (value := getattr(row, field)) is not None]


async def _fail_agent_runs(
    session: AsyncSession, organization_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
) -> None:
    """Stop the agent runs whose approval lapsed.

    ``awaiting_approval`` is in the ``WHERE`` clause: a run that has somehow
    moved on already - resumed by a decision that won the race, cancelled, swept
    - is left exactly where it is. Expiry may end a *paused* run and nothing
    else.
    """
    if not run_ids:
        return

    await session.execute(
        update(AgentRunRecord)
        .where(
            AgentRunRecord.organization_id == organization_id,
            AgentRunRecord.id.in_(tuple(run_ids)),
            AgentRunRecord.status == AgentRunStatus.AWAITING_APPROVAL,
        )
        .values(
            status=AgentRunStatus.FAILED,
            error_code=EXPIRED_ERROR_CODE,
            error_message=EXPIRED_ERROR_MESSAGE,
            completed_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )


async def _fail_workflow_runs(
    session: AsyncSession, organization_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
) -> None:
    """Stop the workflow runs whose approval lapsed."""
    if not run_ids:
        return

    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.organization_id == organization_id,
            WorkflowRun.id.in_(tuple(run_ids)),
            WorkflowRun.status == RunStatus.AWAITING_APPROVAL,
        )
        .values(
            status=RunStatus.FAILED,
            error_code=EXPIRED_ERROR_CODE,
            error_message=EXPIRED_ERROR_MESSAGE,
            completed_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )


async def _fail_step_runs(
    session: AsyncSession, organization_id: uuid.UUID, step_run_ids: Sequence[uuid.UUID]
) -> None:
    """Close the step that was waiting, so the run reads correctly end to end."""
    if not step_run_ids:
        return

    await session.execute(
        update(WorkflowStepRun)
        .where(
            WorkflowStepRun.organization_id == organization_id,
            WorkflowStepRun.id.in_(tuple(step_run_ids)),
            WorkflowStepRun.status == StepRunStatus.AWAITING_APPROVAL,
        )
        .values(
            status=StepRunStatus.FAILED,
            error_code=EXPIRED_ERROR_CODE,
            error_message=EXPIRED_ERROR_MESSAGE,
            completed_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
