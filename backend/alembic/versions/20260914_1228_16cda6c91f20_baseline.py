"""baseline

The empty starting point of the migration history. No tables exist yet; the
first real schema arrives with the tenants table in the multi-tenancy phase.

Running ``alembic upgrade head`` against a fresh database creates the
``alembic_version`` table and stamps it with this revision, so later migrations
have a known base to build on.

Revision ID: 16cda6c91f20
Revises:
Created: 2026-09-14 12:28:48.045812+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "16cda6c91f20"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
