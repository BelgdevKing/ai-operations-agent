"""workflow engine

Revision ID: 503b5bff04ed
Revises: 663cd44adf76
Created: 2026-09-15 07:53:00.000000+00:00

No new tables. The schema phase created ``workflows``, ``workflow_steps``,
``workflow_runs`` and ``workflow_step_runs`` and nothing ever wrote to them; this
revision makes three of them usable and leaves the fourth alone.

    workflows           + version, status, definition;  - enabled
    workflow_runs       + version, user, input/output, idempotency, current step
    workflow_step_runs  + organization, step key, type, position, agent run
    tool_executions     + workflow_step_run_id; run_id becomes nullable
    approvals           + workflow_run_id, workflow_step_run_id

**Ordering matters, and autogenerate does not get it right.** ``approvals`` and
``tool_executions`` both reference ``workflow_step_runs(id, organization_id)``,
so that unique constraint has to exist before either key is created - and has to
outlive both on the way back down. Everything below is sequenced explicitly.

**The migration refuses rather than guesses.** ``workflow_runs`` gains a
``user_id`` and ``workflow_step_runs`` gains an ``organization_id``, both NOT
NULL and neither inventable for a row that already exists. No application code
has ever written to those tables, so in practice they are empty - but "in
practice" is not something a migration should assume, so it checks and stops
with an explanation instead of failing on a constraint or, worse, filling the
columns with something plausible.

**The downgrade removes workflow runs.** A run carries a version, a user, a step
key and possibly the status ``awaiting_approval``, none of which the earlier
schema can hold. Rather than silently reshape somebody's execution history into
something untrue, the downgrade deletes it - and only it. Definitions, and every
other table, survive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "503b5bff04ed"
down_revision: str | None = "663cd44adf76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The longest value in either status enum is "awaiting_approval".
STATUS_LENGTH = 17

RUN_STATUSES = ("pending", "running", "awaiting_approval", "succeeded", "failed", "cancelled")
STEP_STATUSES = (
    "pending",
    "running",
    "awaiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
)
WORKFLOW_STATUSES = ("draft", "active", "inactive")

# What the schema phase created, so the downgrade can put it back exactly.
OLD_RUN_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
OLD_STEP_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")
OLD_STATUS_LENGTH = 9


def _in_list(values: Sequence[str]) -> str:
    return "status IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _require_empty(table: str) -> None:
    """Stop if a table this revision reshapes already has rows.

    The columns being added cannot be derived from what is there - a run has no
    user to attribute it to, a step has no organization - and inventing either
    would put a falsehood in a durable record. Refusing is the honest answer.
    """
    count = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
    if count:
        raise RuntimeError(
            f"{table} contains {count} row(s), which this revision cannot migrate: "
            "the columns it adds are not derivable from existing data. No "
            "application code has ever written to this table, so these rows were "
            "added by hand. Remove or export them, then run the migration again."
        )


def upgrade() -> None:
    _require_empty("workflow_runs")
    _require_empty("workflow_step_runs")

    # -- workflows: a definition, and a version of it ----------------------
    op.add_column(
        "workflows", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )
    op.add_column(
        "workflows",
        sa.Column(
            "status",
            sa.Enum(
                *WORKFLOW_STATUSES,
                name="workflow_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="draft",
            nullable=False,
        ),
    )
    op.add_column(
        "workflows",
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # Existing rows become drafts whatever `enabled` said. They have no
    # definition to run, so calling one "active" would make it startable and
    # immediately unstartable - a draft is what they actually are.
    op.execute(sa.text("UPDATE workflows SET status = 'draft'"))

    op.drop_index(op.f("ix_workflows_organization_id_enabled"), table_name="workflows")
    op.drop_constraint(op.f("uq_workflows_organization_id_name"), "workflows", type_="unique")
    op.create_unique_constraint(
        "uq_workflows_organization_name_version", "workflows", ["organization_id", "name", "version"]
    )
    # Referenced by the runs' composite key.
    op.create_unique_constraint(
        "uq_workflows_id_organization_id", "workflows", ["id", "organization_id"]
    )
    op.create_check_constraint("version_positive", "workflows", "version >= 1")
    op.create_index(
        "ix_workflows_organization_id_status", "workflows", ["organization_id", "status"]
    )
    op.create_index("ix_workflows_organization_id_name", "workflows", ["organization_id", "name"])
    op.drop_column("workflows", "enabled")

    # -- workflow_runs -----------------------------------------------------
    op.drop_constraint(
        op.f("fk_workflow_runs_workflow_id_workflows"), "workflow_runs", type_="foreignkey"
    )
    op.drop_index(op.f("ix_workflow_runs_workflow_id"), table_name="workflow_runs")

    op.add_column(
        "workflow_runs",
        sa.Column("workflow_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("workflow_runs", sa.Column("user_id", sa.UUID(), nullable=False))
    op.add_column("workflow_runs", sa.Column("current_step", sa.String(length=64), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("step_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("input_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("output_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "workflow_runs", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.add_column("workflow_runs", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.add_column("workflow_runs", sa.Column("error_code", sa.String(length=100), nullable=True))

    _widen_status("workflow_runs", RUN_STATUSES)

    op.create_foreign_key(
        op.f("fk_workflow_runs_user_id_users"),
        "workflow_runs",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_workflow_runs_workflow_id_organization_id_workflows",
        "workflow_runs",
        "workflows",
        ["workflow_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    # The idempotency guarantee. NULL keys never collide.
    op.create_unique_constraint(
        "uq_workflow_runs_organization_id_idempotency_key",
        "workflow_runs",
        ["organization_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_workflow_runs_id_organization_id", "workflow_runs", ["id", "organization_id"]
    )
    op.create_check_constraint("step_count_not_negative", "workflow_runs", "step_count >= 0")
    op.create_check_constraint(
        "workflow_version_positive", "workflow_runs", "workflow_version >= 1"
    )
    op.create_index(
        "ix_workflow_runs_workflow_id_organization_id",
        "workflow_runs",
        ["workflow_id", "organization_id"],
    )
    op.create_index("ix_workflow_runs_user_id", "workflow_runs", ["user_id"])
    # The abandonment sweep.
    op.create_index(
        "ix_workflow_runs_status_updated_at", "workflow_runs", ["status", "updated_at"]
    )

    # -- workflow_step_runs ------------------------------------------------
    op.drop_constraint(
        op.f("fk_workflow_step_runs_workflow_run_id_workflow_runs"),
        "workflow_step_runs",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_workflow_step_runs_workflow_run_id"), table_name="workflow_step_runs")
    op.drop_index(
        op.f("ix_workflow_step_runs_workflow_run_id_status"), table_name="workflow_step_runs"
    )

    op.add_column("workflow_step_runs", sa.Column("organization_id", sa.UUID(), nullable=False))
    op.add_column("workflow_step_runs", sa.Column("step_key", sa.String(length=64), nullable=False))
    op.add_column(
        "workflow_step_runs",
        sa.Column(
            "step_type",
            sa.Enum(
                "tool_call",
                "agent_step",
                "approval",
                "condition",
                name="step_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )
    op.add_column("workflow_step_runs", sa.Column("position", sa.Integer(), nullable=False))
    op.add_column("workflow_step_runs", sa.Column("agent_run_id", sa.UUID(), nullable=True))
    op.add_column(
        "workflow_step_runs", sa.Column("error_code", sa.String(length=100), nullable=True)
    )
    # The definition is a document now, so there is no row for a step to point
    # at - see app/models/workflow.py.
    op.alter_column(
        "workflow_step_runs", "workflow_step_id", existing_type=sa.UUID(), nullable=True
    )

    _widen_status("workflow_step_runs", STEP_STATUSES)

    op.create_foreign_key(
        op.f("fk_workflow_step_runs_organization_id_organizations"),
        "workflow_step_runs",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_workflow_step_runs_workflow_run_id_organization_id_runs",
        "workflow_step_runs",
        "workflow_runs",
        ["workflow_run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_workflow_step_runs_agent_run_id_organization_id_agent_runs",
        "workflow_step_runs",
        "agent_runs",
        ["agent_run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="SET NULL",
    )
    # One row per step of a run: this is what "a step cannot execute twice"
    # rests on.
    op.create_unique_constraint(
        "uq_workflow_step_runs_run_id_step_key",
        "workflow_step_runs",
        ["workflow_run_id", "step_key"],
    )
    op.create_unique_constraint(
        "uq_workflow_step_runs_run_id_position",
        "workflow_step_runs",
        ["workflow_run_id", "position"],
    )
    # Referenced by tool_executions and approvals, below.
    op.create_unique_constraint(
        "uq_workflow_step_runs_id_organization_id",
        "workflow_step_runs",
        ["id", "organization_id"],
    )
    op.create_check_constraint("position_positive", "workflow_step_runs", "position >= 1")
    op.create_index(
        op.f("ix_workflow_step_runs_organization_id"), "workflow_step_runs", ["organization_id"]
    )
    op.create_index(
        "ix_workflow_step_runs_workflow_run_id_organization_id",
        "workflow_step_runs",
        ["workflow_run_id", "organization_id"],
    )
    op.create_index(
        "ix_workflow_step_runs_organization_id_status",
        "workflow_step_runs",
        ["organization_id", "status"],
    )

    # -- tool_executions: a workflow step is the other kind of caller ------
    op.add_column(
        "tool_executions", sa.Column("workflow_step_run_id", sa.UUID(), nullable=True)
    )
    op.alter_column("tool_executions", "run_id", existing_type=sa.UUID(), nullable=True)
    op.create_unique_constraint(
        "uq_tool_executions_workflow_step_run_id", "tool_executions", ["workflow_step_run_id"]
    )
    op.create_foreign_key(
        "fk_tool_executions_workflow_step_run_id_organization_id_steps",
        "tool_executions",
        "workflow_step_runs",
        ["workflow_step_run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "exactly_one_caller",
        "tool_executions",
        "(run_id IS NULL) <> (workflow_step_run_id IS NULL)",
    )

    # -- approvals: the other kind of process that pauses -------------------
    op.add_column("approvals", sa.Column("workflow_run_id", sa.UUID(), nullable=True))
    op.add_column("approvals", sa.Column("workflow_step_run_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_approvals_workflow_run_id_organization_id",
        "approvals",
        ["workflow_run_id", "organization_id"],
    )
    op.create_unique_constraint(
        "uq_approvals_workflow_step_run_id", "approvals", ["workflow_step_run_id"]
    )
    op.create_foreign_key(
        "fk_approvals_workflow_run_id_organization_id_workflow_runs",
        "approvals",
        "workflow_runs",
        ["workflow_run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_approvals_workflow_step_run_id_organization_id_step_runs",
        "approvals",
        "workflow_step_runs",
        ["workflow_step_run_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # A workflow run carries a version, a user, a step key and possibly the
    # status `awaiting_approval`, none of which the earlier schema can hold.
    # Removing them is the only honest reverse; approvals and tool executions
    # that point at them follow by cascade, and nothing else is touched.
    op.execute(sa.text("DELETE FROM workflow_runs"))
    # Tool executions a workflow made have no agent run to belong to.
    op.execute(sa.text("DELETE FROM tool_executions WHERE run_id IS NULL"))

    # -- approvals ---------------------------------------------------------
    op.drop_constraint(
        "fk_approvals_workflow_step_run_id_organization_id_step_runs",
        "approvals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_approvals_workflow_run_id_organization_id_workflow_runs",
        "approvals",
        type_="foreignkey",
    )
    op.drop_constraint("uq_approvals_workflow_step_run_id", "approvals", type_="unique")
    op.drop_index("ix_approvals_workflow_run_id_organization_id", table_name="approvals")
    op.drop_column("approvals", "workflow_step_run_id")
    op.drop_column("approvals", "workflow_run_id")

    # -- tool_executions ---------------------------------------------------
    op.drop_constraint("exactly_one_caller", "tool_executions", type_="check")
    op.drop_constraint(
        "fk_tool_executions_workflow_step_run_id_organization_id_steps",
        "tool_executions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_tool_executions_workflow_step_run_id", "tool_executions", type_="unique"
    )
    op.alter_column("tool_executions", "run_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("tool_executions", "workflow_step_run_id")

    # -- workflow_step_runs ------------------------------------------------
    op.drop_index("ix_workflow_step_runs_organization_id_status", table_name="workflow_step_runs")
    op.drop_index(
        "ix_workflow_step_runs_workflow_run_id_organization_id", table_name="workflow_step_runs"
    )
    op.drop_index(op.f("ix_workflow_step_runs_organization_id"), table_name="workflow_step_runs")
    op.drop_constraint("position_positive", "workflow_step_runs", type_="check")
    op.drop_constraint(
        "uq_workflow_step_runs_id_organization_id", "workflow_step_runs", type_="unique"
    )
    op.drop_constraint(
        "uq_workflow_step_runs_run_id_position", "workflow_step_runs", type_="unique"
    )
    op.drop_constraint(
        "uq_workflow_step_runs_run_id_step_key", "workflow_step_runs", type_="unique"
    )
    op.drop_constraint(
        "fk_workflow_step_runs_agent_run_id_organization_id_agent_runs",
        "workflow_step_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_workflow_step_runs_workflow_run_id_organization_id_runs",
        "workflow_step_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_workflow_step_runs_organization_id_organizations"),
        "workflow_step_runs",
        type_="foreignkey",
    )

    _widen_status("workflow_step_runs", OLD_STEP_STATUSES, length=OLD_STATUS_LENGTH)

    op.alter_column(
        "workflow_step_runs", "workflow_step_id", existing_type=sa.UUID(), nullable=False
    )
    op.drop_column("workflow_step_runs", "error_code")
    op.drop_column("workflow_step_runs", "agent_run_id")
    op.drop_column("workflow_step_runs", "position")
    op.drop_column("workflow_step_runs", "step_type")
    op.drop_column("workflow_step_runs", "step_key")
    op.drop_column("workflow_step_runs", "organization_id")

    op.create_index(
        op.f("ix_workflow_step_runs_workflow_run_id_status"),
        "workflow_step_runs",
        ["workflow_run_id", "status"],
    )
    op.create_index(
        op.f("ix_workflow_step_runs_workflow_run_id"), "workflow_step_runs", ["workflow_run_id"]
    )
    op.create_foreign_key(
        op.f("fk_workflow_step_runs_workflow_run_id_workflow_runs"),
        "workflow_step_runs",
        "workflow_runs",
        ["workflow_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # -- workflow_runs -----------------------------------------------------
    op.drop_index("ix_workflow_runs_status_updated_at", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_user_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workflow_id_organization_id", table_name="workflow_runs")
    op.drop_constraint("workflow_version_positive", "workflow_runs", type_="check")
    op.drop_constraint("step_count_not_negative", "workflow_runs", type_="check")
    op.drop_constraint("uq_workflow_runs_id_organization_id", "workflow_runs", type_="unique")
    op.drop_constraint(
        "uq_workflow_runs_organization_id_idempotency_key", "workflow_runs", type_="unique"
    )
    op.drop_constraint(
        "fk_workflow_runs_workflow_id_organization_id_workflows",
        "workflow_runs",
        type_="foreignkey",
    )
    op.drop_constraint(op.f("fk_workflow_runs_user_id_users"), "workflow_runs", type_="foreignkey")

    _widen_status("workflow_runs", OLD_RUN_STATUSES, length=OLD_STATUS_LENGTH)

    op.drop_column("workflow_runs", "error_code")
    op.drop_column("workflow_runs", "request_id")
    op.drop_column("workflow_runs", "idempotency_key")
    op.drop_column("workflow_runs", "output_data")
    op.drop_column("workflow_runs", "input_data")
    op.drop_column("workflow_runs", "step_count")
    op.drop_column("workflow_runs", "current_step")
    op.drop_column("workflow_runs", "user_id")
    op.drop_column("workflow_runs", "workflow_version")

    op.create_index(op.f("ix_workflow_runs_workflow_id"), "workflow_runs", ["workflow_id"])
    op.create_foreign_key(
        op.f("fk_workflow_runs_workflow_id_workflows"),
        "workflow_runs",
        "workflows",
        ["workflow_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # -- workflows ---------------------------------------------------------
    op.add_column(
        "workflows",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.execute(sa.text("UPDATE workflows SET enabled = (status = 'active')"))

    op.drop_index("ix_workflows_organization_id_name", table_name="workflows")
    op.drop_index("ix_workflows_organization_id_status", table_name="workflows")
    op.drop_constraint("version_positive", "workflows", type_="check")
    op.drop_constraint("uq_workflows_id_organization_id", "workflows", type_="unique")
    op.drop_constraint("uq_workflows_organization_name_version", "workflows", type_="unique")

    # Two versions of one name collapse to one row here, so the old constraint
    # can only be restored after the extra versions are gone. Keeping the
    # highest-numbered one preserves what the platform would have run.
    op.execute(
        sa.text(
            "DELETE FROM workflows w USING workflows keep "
            "WHERE w.organization_id = keep.organization_id AND w.name = keep.name "
            "AND w.version < keep.version"
        )
    )
    op.create_unique_constraint(
        op.f("uq_workflows_organization_id_name"), "workflows", ["organization_id", "name"]
    )
    op.create_index(
        op.f("ix_workflows_organization_id_enabled"), "workflows", ["organization_id", "enabled"]
    )
    op.drop_column("workflows", "definition")
    op.drop_column("workflows", "status")
    op.drop_column("workflows", "version")


def _widen_status(table: str, values: Sequence[str], *, length: int = STATUS_LENGTH) -> None:
    """Rewrite a status column's check constraint and its width.

    A check constraint is rewritten in place - which is the whole reason these
    enums are ``VARCHAR`` with a ``CHECK`` rather than a native PostgreSQL
    ``ENUM``, as ``app/models/enums.py`` says. The column also has to be widened:
    it was sized to the longest value the schema phase knew about, and
    "awaiting_approval" is longer than any of them.
    """
    op.drop_constraint("status", table, type_="check")
    op.alter_column(
        table,
        "status",
        existing_type=sa.String(),
        type_=sa.String(length=length),
        existing_nullable=False,
    )
    op.create_check_constraint("status", table, _in_list(values))
