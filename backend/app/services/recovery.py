"""Cleaning up after runs whose request never came back.

There is no background worker in this architecture. A run is advanced inside the
HTTP request that asked for it, so when that request dies - a process restart, a
dropped connection, a replaced container - the run is simply left where it was,
with nothing to continue it.

**No mid-execution resume.** A run that stopped may have been inside a model call
or inside a tool, and nothing here can tell which, or whether that tool's side
effect happened. Re-running it would be the one mistake this system is built to
avoid. So the only honest remedy is to say what happened::

    status      -> failed
    error_code  -> agent_run_abandoned

and leave starting again to a person, who can see what the run did before it
stopped because every step it took is durable.

**Paused runs are never swept.** ``awaiting_approval`` means somebody was asked
and has not answered. Age says nothing about whether they still intend to; an
approval that should expire has its own column (``approvals.expires_at``) and its
own decision to make, which is not this.

The sweep runs in two places, and both are deliberate. Each durable run starts
with one scoped to the caller's own organization, so an abandoned run stops
holding its idempotency key within one request of somebody noticing. And this
module's ``sweep_every_organization`` is what an operator runs from
``scripts/sweep_abandoned_runs.py`` when they want to clear up the whole
deployment at once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import ACTIVE_STATUSES, AgentRunStatus
from app.core.config import Settings
from app.models.agent_run import AgentRunRecord
from app.repositories.agent_run import ABANDONED_ERROR_CODE, ABANDONED_ERROR_MESSAGE

logger = logging.getLogger(__name__)


async def sweep_every_organization(session: AsyncSession, settings: Settings) -> int:
    """Fail abandoned runs across the whole deployment.

    Not tenant-scoped, and therefore not reachable from the API: this is an
    operator's tool, run from a script with the deployment's own credentials.
    Every request-time sweep goes through ``AgentRunRepository.sweep_abandoned``
    instead, which is confined to one organization.

    Returns how many runs were failed.
    """
    stale_after = timedelta(seconds=settings.agent_run_stale_after_seconds)
    cutoff = datetime.now(UTC) - stale_after

    statement = (
        update(AgentRunRecord)
        .where(
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

    result = cast("CursorResult[Any]", await session.execute(statement))
    failed = result.rowcount or 0

    if failed:
        await session.commit()

    logger.info(
        "Abandonment sweep finished",
        extra={
            "context": {
                "runs_failed": failed,
                "stale_after_seconds": settings.agent_run_stale_after_seconds,
                "code": ABANDONED_ERROR_CODE,
            }
        },
    )
    return failed
