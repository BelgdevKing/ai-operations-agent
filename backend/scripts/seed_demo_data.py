"""Load the demo dataset into the development database.

    python -m scripts.seed_demo_data
    python -m scripts.seed_demo_data --attach-user you@example.com

Run it deliberately. Nothing seeds itself at application start-up: demo records
are for a developer who asked for them, and an application that invents
customers on boot is one nobody can trust in staging.

Safe to run repeatedly - every row's id is derived from its business key, so a
second run refreshes the same records rather than duplicating them.

Refuses to touch anything but a development database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.database import engine
from app.demo.seed import seed_demo_data


async def main(attach_user_email: str | None) -> int:
    settings = get_settings()

    # A guard rather than a comment. Synthetic customers in a production
    # database would be indistinguishable from real ones a week later.
    if settings.app_env != "development":
        print(
            f"Refusing to seed demo data in APP_ENV={settings.app_env!r}. "
            "This script is for local development only.",
            file=sys.stderr,
        )
        return 1

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        organizations = await seed_demo_data(session, attach_user_email=attach_user_email)
        await session.commit()

    await engine.dispose()

    print("Seeded demo organizations:")
    for slug, organization_id in sorted(organizations.items()):
        print(f"  {slug}  {organization_id}")

    if attach_user_email:
        print(f"\nAttached {attach_user_email} as an owner where that account exists.")

    print(
        "\nTry asking the agent:\n"
        '  "Check shipment ABC123 and tell me if there are outstanding charges."'
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--attach-user",
        dest="attach_user_email",
        default=None,
        help="Make this existing account an owner of the demo organizations, "
        "so you can sign in and see the data. No password is read or stored.",
    )
    arguments = parser.parse_args()

    raise SystemExit(asyncio.run(main(arguments.attach_user_email)))
