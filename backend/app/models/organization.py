"""Organizations and their membership.

The organization is the tenant. Every tenant-owned table carries its id; see
``OrganizationScopedMixin``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    MemberRole,
    MembershipStatus,
    OrganizationStatus,
    enum_column,
)
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[OrganizationStatus] = mapped_column(
        enum_column(OrganizationStatus, "status"),
        nullable=False,
        default=OrganizationStatus.ACTIVE,
        server_default=OrganizationStatus.ACTIVE.value,
    )

    members: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_organizations_status", "status"),)


class OrganizationMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user's membership of one organization, carrying their role in it.

    Users are global: the same person can belong to several organizations with
    a different role in each, which is why the role lives here and not on
    ``users``.
    """

    __tablename__ = "organization_members"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MemberRole] = mapped_column(
        enum_column(MemberRole, "role"),
        nullable=False,
        default=MemberRole.MEMBER,
        server_default=MemberRole.MEMBER.value,
    )
    status: Mapped[MembershipStatus] = mapped_column(
        enum_column(MembershipStatus, "status"),
        nullable=False,
        default=MembershipStatus.ACTIVE,
        server_default=MembershipStatus.ACTIVE.value,
    )

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        # A user holds at most one role per organization.
        UniqueConstraint("organization_id", "user_id", name="uq_organization_members_org_user"),
        Index("ix_organization_members_organization_id", "organization_id"),
        Index("ix_organization_members_user_id", "user_id"),
        # Listing the active members of an organization is the common query.
        Index("ix_organization_members_organization_id_status", "organization_id", "status"),
    )
