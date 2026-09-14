"""User accounts.

Users are global rather than tenant-owned: one person can belong to several
organizations. What they may do is decided by their ``OrganizationMember`` row
in the organization being accessed, not by anything on this table.

Authentication is not implemented yet. ``password_hash`` is the column the
later phase will populate; nothing here hashes, verifies or issues anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import UserStatus, enum_column
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import OrganizationMember


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person who can sign in and belong to organizations."""

    __tablename__ = "users"

    # 320 is the maximum length an email address can have per RFC 5321.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)

    # Nullable: an invited user exists before they have ever set a password.
    # Populated by the authentication phase; never read or written here.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, "status"),
        nullable=False,
        default=UserStatus.PENDING,
        server_default=UserStatus.PENDING.value,
    )

    memberships: Mapped[list[OrganizationMember]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_users_status", "status"),)
