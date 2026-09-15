"""durable agent execution and approvals

Revision ID: 663cd44adf76
Revises: 3679391a4d0d
Created: 2026-09-15 05:04:49.262094+00:00

Three new tables and three altered ones.

New
    agent_runs        the authoritative record of one execution
    agent_steps       one model call each
    tool_executions   one tool attempt each, and the at-most-once flag

Altered
    audit_events      created_at defaults to clock_timestamp(), so the trail is ordered
    approvals         gains run_id, tool_execution_id and tool_name
    conversations     agent_id becomes nullable; gains UNIQUE(id, organization_id)
    messages          gains tool_metadata and sequence

**Order matters in both directions, and autogenerate does not get it right.**
``agent_runs`` carries a composite foreign key to ``conversations(id,
organization_id)``, so the unique constraint that key references has to exist
before the table does; and on the way back down the table has to go before the
constraint it depends on. Both are sequenced explicitly below.

**Existing data is not assumed to be absent.** Every change to ``approvals``,
``conversations`` and ``messages`` adds a nullable column or relaxes a
constraint, so a populated database upgrades without a backfill and without a
rewrite.

**The downgrade deletes rows it cannot represent.** Making
``conversations.agent_id`` nullable is what lets a conversation be stored for the
platform agent, which has no row in ``agents``; restoring ``NOT NULL`` means
those conversations cannot exist. They are removed, and their messages and
approvals go with them by cascade. Nothing created before this revision is
touched - every such conversation was written by the durable-execution feature
this revision adds.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "663cd44adf76"
down_revision: str | None = "3679391a4d0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- conversations, first ---------------------------------------------
    #
    # Both changes have to land before agent_runs is created: the unique
    # constraint is what its composite foreign key references, and the
    # nullability is what lets a conversation exist for a platform agent.
    op.alter_column("conversations", "agent_id", existing_type=sa.UUID(), nullable=True)
    op.create_unique_constraint(
        "uq_conversations_id_organization_id", "conversations", ["id", "organization_id"]
    )

    op.add_column(
        "messages",
        sa.Column("tool_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # A conversation's order has to be a total order, and ``created_at`` is not
    # one: PostgreSQL holds ``now()`` constant for a transaction, so turns written
    # together share a timestamp and the tie-break falls to a random primary key.
    # Added with a server default so existing rows are valid immediately, then
    # backfilled to the order they were already being read in - which leaves
    # every existing conversation reading exactly as it did before.
    op.add_column(
        "messages",
        sa.Column("sequence", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.execute(
        sa.text(
            "UPDATE messages SET sequence = ordered.position FROM ("
            "  SELECT id, row_number() OVER ("
            "    PARTITION BY conversation_id ORDER BY created_at, id"
            "  ) AS position FROM messages"
            ") AS ordered WHERE messages.id = ordered.id"
        )
    )
    op.create_index(
        "ix_messages_conversation_id_sequence", "messages", ["conversation_id", "sequence"]
    )

    # -- audit_events ------------------------------------------------------
    #
    # The trail gained enough events per run that its order started to matter,
    # and ``now()`` cannot provide one: it is the transaction timestamp, so
    # events written by a single unit of work share it and sort by a random
    # primary key. ``clock_timestamp()`` advances within a transaction.
    #
    # Existing rows are untouched - the default only applies to new ones.
    op.alter_column(
        "audit_events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=sa.text("clock_timestamp()"),
    )

    # -- agent_runs --------------------------------------------------------
    op.create_table(
        "agent_runs",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "awaiting_approval",
                "completed",
                "failed",
                "cancelled",
                name="agent_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("step_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("step_count >= 0", name=op.f("ck_agent_runs_step_count_not_negative")),
        sa.CheckConstraint(
            "tool_call_count >= 0", name=op.f("ck_agent_runs_tool_call_count_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            name="fk_agent_runs_conversation_id_organization_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_runs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_agent_runs_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.UniqueConstraint("id", "organization_id", name="uq_agent_runs_id_organization_id"),
        # The idempotency guarantee. NULL keys never collide, so runs made
        # without one are unaffected.
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_agent_runs_organization_id_idempotency_key",
        ),
    )
    op.create_index(
        "ix_agent_runs_conversation_id_organization_id",
        "agent_runs",
        ["conversation_id", "organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_runs_organization_id"), "agent_runs", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_agent_runs_organization_id_created_at",
        "agent_runs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_organization_id_status",
        "agent_runs",
        ["organization_id", "status"],
        unique=False,
    )
    # The abandonment sweep reads this one.
    op.create_index(
        "ix_agent_runs_status_updated_at", "agent_runs", ["status", "updated_at"], unique=False
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"], unique=False)

    # -- agent_steps -------------------------------------------------------
    op.create_table(
        "agent_steps",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("step_number >= 1", name=op.f("ck_agent_steps_step_number_positive")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_steps_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            name="fk_agent_steps_run_id_organization_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        # One row per step of a run: a replayed request cannot record work twice.
        sa.UniqueConstraint("run_id", "step_number", name="uq_agent_steps_run_id_step_number"),
    )
    op.create_index(
        op.f("ix_agent_steps_organization_id"), "agent_steps", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_agent_steps_organization_id_created_at",
        "agent_steps",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_steps_run_id_organization_id",
        "agent_steps",
        ["run_id", "organization_id"],
        unique=False,
    )

    # -- tool_executions ---------------------------------------------------
    op.create_table(
        "tool_executions",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("safety", sa.String(length=16), nullable=True),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        # The at-most-once flag, raised by a conditional update before the tool
        # body is entered.
        sa.Column("executed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("argument_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("result_field_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "argument_count >= 0", name=op.f("ck_tool_executions_argument_count_not_negative")
        ),
        sa.CheckConstraint(
            "step_number >= 1", name=op.f("ck_tool_executions_step_number_positive")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_executions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            ["agent_runs.id", "agent_runs.organization_id"],
            name="fk_tool_executions_run_id_organization_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_executions")),
        sa.UniqueConstraint("id", "organization_id", name="uq_tool_executions_id_organization_id"),
        sa.UniqueConstraint("run_id", "step_number", name="uq_tool_executions_run_id_step_number"),
    )
    op.create_index(
        op.f("ix_tool_executions_organization_id"),
        "tool_executions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_executions_organization_id_created_at",
        "tool_executions",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tool_executions_organization_id_tool_name",
        "tool_executions",
        ["organization_id", "tool_name"],
        unique=False,
    )
    op.create_index(
        "ix_tool_executions_run_id_organization_id",
        "tool_executions",
        ["run_id", "organization_id"],
        unique=False,
    )

    # -- approvals ---------------------------------------------------------
    #
    # Three nullable columns and two composite foreign keys. Existing rows keep
    # working untouched: a workflow approval simply leaves all three null.
    op.add_column("approvals", sa.Column("run_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("tool_execution_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("tool_name", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_approvals_run_id_organization_id",
        "approvals",
        ["run_id", "organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_approvals_tool_execution_id_organization_id",
        "approvals",
        ["tool_execution_id", "organization_id"],
        unique=False,
    )
    # One approval per execution. NULLs do not collide, so workflow approvals
    # are unaffected.
    op.create_unique_constraint(
        "uq_approvals_tool_execution_id", "approvals", ["tool_execution_id"]
    )
    op.create_foreign_key(
        "fk_approvals_run_id_organization_id_agent_runs",
        "approvals",
        "agent_runs",
        ["run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_approvals_tool_execution_id_organization_id_tool_executions",
        "approvals",
        "tool_executions",
        ["tool_execution_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # -- approvals ---------------------------------------------------------
    op.drop_constraint(
        "fk_approvals_tool_execution_id_organization_id_tool_executions",
        "approvals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_approvals_run_id_organization_id_agent_runs", "approvals", type_="foreignkey"
    )
    op.drop_constraint("uq_approvals_tool_execution_id", "approvals", type_="unique")
    op.drop_index("ix_approvals_tool_execution_id_organization_id", table_name="approvals")
    op.drop_index("ix_approvals_run_id_organization_id", table_name="approvals")
    op.drop_column("approvals", "tool_name")
    op.drop_column("approvals", "tool_execution_id")
    op.drop_column("approvals", "run_id")

    # -- the execution tables, children first ------------------------------
    op.drop_index("ix_tool_executions_run_id_organization_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_organization_id_tool_name", table_name="tool_executions")
    op.drop_index("ix_tool_executions_organization_id_created_at", table_name="tool_executions")
    op.drop_index(op.f("ix_tool_executions_organization_id"), table_name="tool_executions")
    op.drop_table("tool_executions")

    op.drop_index("ix_agent_steps_run_id_organization_id", table_name="agent_steps")
    op.drop_index("ix_agent_steps_organization_id_created_at", table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_organization_id"), table_name="agent_steps")
    op.drop_table("agent_steps")

    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status_updated_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_organization_id_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_organization_id_created_at", table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_organization_id"), table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversation_id_organization_id", table_name="agent_runs")
    # Must go before the unique constraint its composite key references.
    op.drop_table("agent_runs")

    # -- conversations and messages, last ----------------------------------
    op.drop_index("ix_messages_conversation_id_sequence", table_name="messages")
    op.drop_column("messages", "sequence")
    op.drop_column("messages", "tool_metadata")
    op.drop_constraint("uq_conversations_id_organization_id", "conversations", type_="unique")

    # A conversation with no agent cannot be represented before this revision.
    # Those rows exist only because this revision made them possible - a
    # conversation served by the platform agent, which has no row in ``agents``.
    # They are removed so the column can be NOT NULL again; their messages and
    # approvals follow by cascade. Conversations that predate this revision all
    # have an agent and are untouched.
    op.execute(sa.text("DELETE FROM conversations WHERE agent_id IS NULL"))
    op.alter_column("conversations", "agent_id", existing_type=sa.UUID(), nullable=False)

    op.alter_column(
        "audit_events",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=sa.text("now()"),
    )
