"""Workflows: definitions, their steps, and the record of executing them.

Definitions and history only. No state machine, no scheduler, no execution -
those arrive with the workflow phase. ``workflow_runs`` and
``workflow_step_runs`` are the tables that engine will write to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import RunStatus, StepRunStatus, WorkflowStepType, enum_column
from app.models.mixins import OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization


class Workflow(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A named, ordered procedure belonging to one organization."""

    __tablename__ = "workflows"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    organization: Mapped[Organization] = relationship()

    steps: Mapped[list[WorkflowStep]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowStep.step_order",
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_workflows_organization_id_name"),
        Index("ix_workflows_organization_id_enabled", "organization_id", "enabled"),
    )


class WorkflowStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One step of a workflow definition.

    Reaches its tenant through the workflow, so it carries no
    ``organization_id`` of its own.
    """

    __tablename__ = "workflow_steps"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    step_type: Mapped[WorkflowStepType] = mapped_column(
        enum_column(WorkflowStepType, "step_type"),
        nullable=False,
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # Shape depends on step_type - which tool, which agent, what condition.
    # JSONB rather than a column per step type, because the engine validates
    # it against the step type at execution time.
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    workflow: Mapped[Workflow] = relationship(back_populates="steps")

    __table_args__ = (
        # Two steps cannot claim the same position, which is what makes the
        # order deterministic.
        UniqueConstraint("workflow_id", "step_order", name="uq_workflow_steps_workflow_id_order"),
        CheckConstraint("step_order >= 0", name="step_order_non_negative"),
        Index("ix_workflow_steps_workflow_id", "workflow_id"),
    )


class WorkflowRun(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One execution of a workflow.

    Carries ``organization_id`` directly, even though it could be reached
    through the workflow: runs are queried and listed per tenant constantly, and
    the join would be on the hot path for every such query.
    """

    __tablename__ = "workflow_runs"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, "status"),
        nullable=False,
        default=RunStatus.PENDING,
        server_default=RunStatus.PENDING.value,
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped[Organization] = relationship()
    workflow: Mapped[Workflow] = relationship()

    step_runs: Mapped[list[WorkflowStepRun]] = relationship(
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_workflow_runs_workflow_id", "workflow_id"),
        # The run list and the "what is still running?" query, per tenant.
        Index("ix_workflow_runs_organization_id_status", "organization_id", "status"),
        Index("ix_workflow_runs_organization_id_created_at", "organization_id", "created_at"),
    )


class WorkflowStepRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One step's execution within a run.

    Not unique on (run, step): a step may be retried, and each attempt is its
    own row so the history stays complete.
    """

    __tablename__ = "workflow_step_runs"

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_step_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_steps.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[StepRunStatus] = mapped_column(
        enum_column(StepRunStatus, "status"),
        nullable=False,
        default=StepRunStatus.PENDING,
        server_default=StepRunStatus.PENDING.value,
    )

    # Nullable rather than defaulting to {}: "no output yet" and "produced an
    # empty result" are different facts, and a resumed run needs to tell them
    # apart.
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="step_runs")
    step: Mapped[WorkflowStep] = relationship()

    __table_args__ = (
        Index("ix_workflow_step_runs_workflow_run_id", "workflow_run_id"),
        Index("ix_workflow_step_runs_workflow_step_id", "workflow_step_id"),
        Index("ix_workflow_step_runs_workflow_run_id_status", "workflow_run_id", "status"),
    )
