"""Fail agent runs that nothing is driving any more.

    python -m scripts.sweep_abandoned_runs
    python -m scripts.sweep_abandoned_runs --dry-run

There is no background worker in this platform, so a run whose HTTP request died
has nothing to continue it. Each durable run already sweeps its own organization
on the way in, which is enough for a deployment that is being used; this is for
the rest - after an incident, or for an organization nobody has visited since.

**Nothing is resumed.** A run that stopped may have been inside a model call or
inside a tool, and nothing can tell from here whether that tool's side effect
happened. Abandoned runs are marked ``failed`` with ``agent_run_abandoned`` and a
person decides what to do about them.

**Runs awaiting approval are not touched**, however old they are. They are paused
because somebody was asked, and nobody has answered yet.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.models import ACTIVE_STATUSES
from app.core.config import get_settings
from app.core.database import engine
from app.models.agent_run import AgentRunRecord
from app.services.recovery import sweep_every_organization


async def main(dry_run: bool) -> int:
    settings = get_settings()
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    window = settings.agent_run_stale_after_seconds
    print(f"Runs with no progress for {window}s are considered abandoned.")

    try:
        async with factory() as session:
            if dry_run:
                cutoff = datetime.now(UTC) - timedelta(seconds=window)
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(AgentRunRecord)
                        .where(
                            AgentRunRecord.status.in_(tuple(ACTIVE_STATUSES)),
                            AgentRunRecord.updated_at < cutoff,
                        )
                    )
                ).scalar_one()
                print(f"Would fail {count} run(s). Nothing was changed.")
                return 0

            failed = await sweep_every_organization(session, settings)
            print(f"Failed {failed} abandoned run(s) with code agent_run_abandoned.")
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be failed without changing anything.",
    )
    arguments = parser.parse_args()

    sys.exit(asyncio.run(main(arguments.dry_run)))
