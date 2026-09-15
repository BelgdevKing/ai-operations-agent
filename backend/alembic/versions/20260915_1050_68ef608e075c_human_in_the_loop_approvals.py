"""Human-in-the-loop: what an approver is told, and what they wrote back.

Three columns on one existing table, and nothing else. The human-in-the-loop
phase is mostly *behaviour* built on the approval infrastructure the durable
execution and workflow phases already put in place - the lifecycle states, the
conditional decision, the tenant-carrying keys and the ``expires_at`` column all
exist already, and ``ApprovalStatus`` has had ``expired`` and ``cancelled`` since
the schema phase. What it did not have was anything to show a reviewer beyond a
tool's name, or anywhere to keep what the reviewer said.

    summary          "Cancel shipment ABC123" - one line, bounded
    summary_fields   the labelled values behind it, as [{"label", "value"}]
    decision_reason  what the deciding person typed, if anything

``summary_fields`` deserves a note, because it is the one column here that holds
anything resembling business data. It is not a copy of the tool's arguments.
Each tool declares, in its own code, which of its fields an approver may be
shown; that allow-list is applied once, when the approval is requested, and only
its output is written. The column therefore holds a projection the rest of the
payload was never part of - which is a different thing from a payload with some
fields redacted, and the difference is the whole point. ``parameters`` remains
unused by agent approvals, as it has been since it was written.

**Upgrade is additive and safe on a populated table.** All three columns are
nullable or carry a server default, so existing approvals acquire them without a
rewrite and without a lock beyond the catalogue update. An approval that predates
this migration simply has no summary and no decision reason, which is exactly
what was true of it.

**Downgrade loses what it drops, and there is no way for it not to.** The three
columns have nowhere else to go: a decision reason is a sentence somebody wrote
about a specific business action and has no representation in the earlier schema.
Approvals themselves, their decisions, their links and their deadlines all
survive - only the added detail is lost. No other table is touched in either
direction.

Revision ID: 68ef608e075c
Revises: 503b5bff04ed
Created: 2026-09-15 10:50:24.869076+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "68ef608e075c"
down_revision: str | None = "503b5bff04ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The headline. Bounded at the width the response publishes it at, so a
    # summary can never be longer in the database than an interface will show.
    op.add_column("approvals", sa.Column("summary", sa.String(length=200), nullable=True))

    # The labelled values behind it. NOT NULL with an empty-array default: an
    # approval either has a summary or has no fields, and a nullable list would
    # make "no fields" and "nobody has written any yet" indistinguishable for no
    # benefit. Existing rows get [] without being rewritten.
    op.add_column(
        "approvals",
        sa.Column(
            "summary_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )

    # What the person who decided wrote. Nullable, because a decision needs no
    # explanation to be valid; bounded, because it arrives from a browser.
    op.add_column("approvals", sa.Column("decision_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    # Reverse order of creation. See the module docstring: this discards the
    # summaries and the decision reasons, and nothing else.
    op.drop_column("approvals", "decision_reason")
    op.drop_column("approvals", "summary_fields")
    op.drop_column("approvals", "summary")
