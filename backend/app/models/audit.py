"""Append-only audit trail.

Rows are written and never updated or deleted, so the table carries only
``created_at`` - and that column is the whole order of the trail, which is why
it is the one place in the schema that does not use ``now()``.

``now()`` is the *transaction* timestamp: PostgreSQL holds it constant from
BEGIN to COMMIT, so several events written by one unit of work would share it and
their order would fall to a random primary key. A trail that cannot say whether
the approval came before or after the run finished is not much of a trail.
``clock_timestamp()`` advances within a transaction, which is what an append-only
log actually wants.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import CreatedAtMixin, OrganizationScopedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class AuditEvent(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """One recorded action."""

    __tablename__ = "audit_events"

    # Overrides CreatedAtMixin's now(). See the module docstring: this column is
    # the order of the trail, and a transaction-constant clock cannot provide one.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        # Nullable and SET NULL: an agent or a scheduled job has no user, and
        # the trail must survive the removal of an account.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)

    # Which entity was acted on. Deliberately not a foreign key: the trail must
    # outlive the row it describes, and it spans every table.
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    action: Mapped[str] = mapped_column(String(100), nullable=False)

    # Mapped to the column "metadata", but named event_metadata in Python:
    # "metadata" is reserved on a declarative class, where it is the MetaData
    # object, and using it as an attribute raises at class definition time.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    organization: Mapped[Organization] = relationship()
    user: Mapped[User | None] = relationship()

    __table_args__ = (
        # The audit view is one tenant's history, newest first.
        Index("ix_audit_events_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_events_organization_id_event_type", "organization_id", "event_type"),
        Index("ix_audit_events_user_id", "user_id"),
        # "What happened to this record?" across the whole trail.
        Index("ix_audit_events_resource_type_resource_id", "resource_type", "resource_id"),
    )
