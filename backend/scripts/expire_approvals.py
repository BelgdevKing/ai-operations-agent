"""Lapse approvals nobody answered, and stop the runs they were holding.

    python -m scripts.expire_approvals
    python -m scripts.expire_approvals --dry-run

There is no background worker in this platform, so an approval lapses the next
time something looks at it. Reading the queue does that for one organization,
and trying to decide something does it for one approval - which covers any
organization that is being used at all. This is for the rest: an organization
nobody has visited this week, where a paused run is quietly holding a
conversation and a claimed execution that will never be needed.

**Expiring is not deciding.** The approval becomes ``expired``, never
``rejected`` - nobody refused it - and the run it paused is failed with
``approval_expired`` rather than being sent down whatever path the business
process declared for a human "no". The gated action does not run, here or ever
after: the deadline is checked in the same statement that records a decision, so
it has not been decidable since the moment it passed, whatever its row still
said.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.database import engine
from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.services.approval_expiry import EXPIRED_ERROR_CODE, expire_every_organization


async def main(dry_run: bool) -> int:
    settings = get_settings()
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    window = settings.approval_expiration_seconds
    print(f"Approvals are given {window}s to be decided.")

    try:
        async with factory() as session:
            if dry_run:
                # The same condition the sweep uses, counted instead of applied.
                # `now()` rather than a Python clock, so a dry run and the real
                # one are answering the question against the same clock.
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.status == ApprovalStatus.PENDING,
                            Approval.expires_at.is_not(None),
                            Approval.expires_at <= func.now(),
                        )
                    )
                ).scalar_one()
                print(f"Would expire {count} approval(s). Nothing was changed.")
                return 0

            expired = await expire_every_organization(session)
            print(f"Expired {expired} approval(s); paused runs stopped with {EXPIRED_ERROR_CODE}.")
    finally:
        await engine.dispose()

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be expired without changing anything.",
    )
    arguments = parser.parse_args()

    sys.exit(asyncio.run(main(arguments.dry_run)))
