"""Human approval requests for sensitive agent actions.

The record only. The gate that creates these rows, pauses a run and resumes it
on approval is the approvals phase.

The design point worth preserving: ``parameters`` stores the exact arguments the
action was requested with, so approving resumes the original call rather than
letting the model re-derive it afterwards.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ApprovalStatus, enum_column
from app.models.mixins import OrganizationScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.agent import Agent, Tool
    from app.models.conversation import Conversation
    from app.models.organization import Organization
    from app.models.user import User


class Approval(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A request for a human to authorise an action before it runs."""

    __tablename__ = "approvals"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: who asked is part of the record and must not disappear.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # All three are nullable: an approval can come from a workflow step rather
    # than a conversation, and the action need not involve a registered tool.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="RESTRICT"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )

    action: Mapped[str] = mapped_column(String(200), nullable=False)

    # The exact arguments to replay on approval.
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    # Why the action was gated, shown to the approver.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
        server_default=ApprovalStatus.PENDING.value,
    )

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship()
    requester: Mapped[User] = relationship(foreign_keys=[requested_by])
    approver: Mapped[User | None] = relationship(foreign_keys=[approved_by])
    agent: Mapped[Agent | None] = relationship()
    tool: Mapped[Tool | None] = relationship()
    conversation: Mapped[Conversation | None] = relationship()

    __table_args__ = (
        # The approval queue: pending items for one organization.
        Index("ix_approvals_organization_id_status", "organization_id", "status"),
        Index("ix_approvals_requested_by", "requested_by"),
        Index("ix_approvals_approved_by", "approved_by"),
        Index("ix_approvals_agent_id", "agent_id"),
        Index("ix_approvals_tool_id", "tool_id"),
        Index("ix_approvals_conversation_id", "conversation_id"),
        # Sweeping expired requests looks only at pending ones.
        Index("ix_approvals_status_expires_at", "status", "expires_at"),
    )
