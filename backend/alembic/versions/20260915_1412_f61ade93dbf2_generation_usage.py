"""Usage from direct generation, which had nowhere durable to go.

``/ai/generate`` calls the configured model and returns the answer. It is not an
agent: it creates no run, no conversation and no tool execution. Every usage
figure this platform reports is aggregated from the execution tables, so the
tokens that endpoint spent were counted by nothing - visible in a metric for the
process, absent from any tenant's bill.

**One table, nine columns, all operational.** No prompt, no completion, no tool
arguments, no tool results, no conversation content, no idempotency key, no
credential - the same rule ``agent_steps`` and ``tool_executions`` follow, and
for the same reason. And no ``provider`` column: which vendor answered is
internal routing, absent from every outward contract since the durable-execution
phase, and the price book is keyed on the model anyway.

**Why not reuse a table.** Writing an ``agent_runs`` row plus an ``agent_steps``
row for each direct generation was the obvious alternative and was rejected:
``agent_steps.run_id`` is ``NOT NULL`` with a composite key into ``agent_runs``,
so the reuse forces a run into existence with a fabricated ``agent_id``, no
conversation to open, and a place in ``GET /ai/runs`` where a user would see a
run they cannot read. That changes what a Part 16 agent run *is* in order to
make a Part 19 report add up. ``audit_events`` could physically hold the figures
in its JSONB, but audit answers "what happened" and usage answers "how much" -
and a token sum aggregated out of JSONB is neither indexed nor honest.

**Two indexes that are the query, not a guess.** ``(organization_id,
created_at)`` is every usage report - one tenant, one window - and
``(organization_id, model)`` is the per-model breakdown. ``UNIQUE(id,
organization_id)`` is redundant against the primary key and present for the Part
14 reason: it is what a future table needs in order to reference this one with a
tenant-carrying composite key, and adding it now costs an index rather than a
migration.

**Upgrade and downgrade are both clean.** The table is new, so the upgrade
touches nothing that exists and needs no backfill - a deployment's history
before this revision simply has no direct-generation usage recorded, which is
the truth. The downgrade drops the table and with it every figure in it;
nothing else is affected, and no other table is touched in either direction.

Revision ID: f61ade93dbf2
Revises: 68ef608e075c
Created: 2026-09-15 14:12:14.484389+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f61ade93dbf2"
down_revision: str | None = "68ef608e075c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Column order follows the model's own: the mixins contribute id,
    # organization_id and created_at, and autogenerate lists them last.
    op.create_table(
        "generation_usage",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_generation_usage_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_generation_usage_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_usage")),
        sa.UniqueConstraint("id", "organization_id", name="uq_generation_usage_id_organization_id"),
    )
    op.create_index(
        op.f("ix_generation_usage_organization_id"),
        "generation_usage",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_usage_organization_id_created_at",
        "generation_usage",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_generation_usage_organization_id_model",
        "generation_usage",
        ["organization_id", "model"],
        unique=False,
    )
    op.create_index("ix_generation_usage_user_id", "generation_usage", ["user_id"], unique=False)


def downgrade() -> None:
    # Reverse order of creation. This discards every recorded direct
    # generation; nothing else is touched.
    op.drop_index("ix_generation_usage_user_id", table_name="generation_usage")
    op.drop_index("ix_generation_usage_organization_id_model", table_name="generation_usage")
    op.drop_index("ix_generation_usage_organization_id_created_at", table_name="generation_usage")
    op.drop_index(op.f("ix_generation_usage_organization_id"), table_name="generation_usage")
    op.drop_table("generation_usage")
