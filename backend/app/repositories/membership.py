"""Organization membership data access.

Memberships are tenant-owned, so every query here is scoped through
:class:`TenantScopedRepository`. The lookups that deliberately span
organizations - answering "which organizations does this user belong to?" -
are on the unscoped :class:`MembershipLookup` below, kept separate so the
scoped surface stays trustworthy.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import MemberRole, MembershipStatus
from app.models.organization import OrganizationMember
from app.repositories.tenant import TenantScopedRepository


class MembershipRepository(TenantScopedRepository[OrganizationMember]):
    """Memberships of one organization."""

    model = OrganizationMember

    async def list_with_users(
        self, *, limit: int = 50, offset: int = 0
    ) -> Sequence[OrganizationMember]:
        """Members of this organization, each with its user loaded.

        Eager-loaded because async SQLAlchemy cannot lazy-load on attribute
        access, and the response needs the user on every row.
        """
        statement = (
            self.select()
            .options(selectinload(OrganizationMember.user))
            .order_by(OrganizationMember.created_at)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_by_user(self, user_id: uuid.UUID) -> OrganizationMember | None:
        """One user's membership of this organization, with the user loaded."""
        statement = (
            self.select()
            .where(OrganizationMember.user_id == user_id)
            .options(selectinload(OrganizationMember.user))
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def count_by_role(self, role: MemberRole) -> int:
        """How many active members hold a role.

        Used to stop the last owner being demoted or removed.
        """
        statement = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(self.tenant_filter)
            .where(OrganizationMember.role == role)
            .where(OrganizationMember.status == MembershipStatus.ACTIVE)
        )
        result = await self.session.execute(statement)
        return result.scalar_one()


class MembershipLookup:
    """Membership queries that intentionally cross organizations.

    Only two callers need this: resolving which organization a request acts on,
    and listing the caller's own memberships. Both answer questions about the
    authenticated user, and both filter by that user's id - which is why they
    cannot be expressed by the scoped repository above.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[OrganizationMember]:
        """Every active membership held by one user, organization loaded."""
        statement = (
            select(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
            .where(OrganizationMember.status == MembershipStatus.ACTIVE)
            .options(selectinload(OrganizationMember.organization))
            .order_by(OrganizationMember.created_at)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def get_for_user_and_organization(
        self, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> OrganizationMember | None:
        """One user's active membership of one organization, or None.

        The authorization primitive: a None here is what turns a request for
        another tenant's data into a refusal.
        """
        statement = (
            select(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
            .where(OrganizationMember.organization_id == organization_id)
            .where(OrganizationMember.status == MembershipStatus.ACTIVE)
            .options(selectinload(OrganizationMember.organization))
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()
