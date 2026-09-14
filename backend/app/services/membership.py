"""Organization membership administration.

Every method operates on one organization, fixed at construction from the
caller's verified membership. No method accepts an organization id, so none can
be pointed at another tenant.

The rules enforced here, in one place so they can be read as a set:

* Nobody changes their own role. Self-promotion is the obvious attack, and
  self-demotion is usually a mistake.
* Only an owner grants or revokes the owner role.
* Only an owner modifies or removes another owner.
* The last active owner cannot be demoted or removed - an organization without
  an owner cannot be administered again.
* Anyone may remove themselves, subject to the rule above.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.enums import MemberRole
from app.models.organization import OrganizationMember
from app.repositories.membership import MembershipRepository

logger = logging.getLogger(__name__)

# Roles permitted to administer membership at all.
ADMINISTRATIVE_ROLES = frozenset({MemberRole.OWNER, MemberRole.ADMIN})


class MembershipService:
    """Membership administration for a single organization."""

    def __init__(self, session: AsyncSession, actor: OrganizationMember) -> None:
        self.session = session
        self.actor = actor
        # Scoped to the actor's own organization; this is what makes every
        # query below tenant-safe.
        self.members = MembershipRepository(session, actor.organization_id)

    async def list_members(
        self, *, limit: int = 50, offset: int = 0
    ) -> Sequence[OrganizationMember]:
        """List the organization's members.

        Readable by any active member: seeing who your colleagues are is
        ordinary use, not administration.
        """
        return await self.members.list_with_users(limit=limit, offset=offset)

    async def change_role(self, user_id: uuid.UUID, new_role: MemberRole) -> OrganizationMember:
        """Change one member's role."""
        self._require_administrator()
        target = await self._require_member(user_id)

        if target.user_id == self.actor.user_id:
            raise PermissionDeniedError("You cannot change your own role.")

        # Granting or revoking ownership is the one privilege an admin does
        # not have; otherwise an admin could make themselves an owner through
        # a second account.
        if MemberRole.OWNER in (target.role, new_role) and self.actor.role is not MemberRole.OWNER:
            raise PermissionDeniedError("Only an owner can grant or revoke the owner role.")

        if target.role is new_role:
            return target

        if target.role is MemberRole.OWNER:
            await self._require_another_owner_remains()

        target.role = new_role
        await self.session.flush()

        logger.info(
            "Changed member role",
            extra={
                "context": {
                    "organization_id": str(self.actor.organization_id),
                    "target_user_id": str(user_id),
                    "new_role": new_role.value,
                }
            },
        )
        return target

    async def remove_member(self, user_id: uuid.UUID) -> None:
        """Remove a member from the organization.

        Removing yourself is allowed - leaving is not an administrative act -
        but the last-owner rule still applies.
        """
        is_self = user_id == self.actor.user_id
        if not is_self:
            self._require_administrator()

        target = await self._require_member(user_id)

        removing_another_owner = (
            not is_self
            and target.role is MemberRole.OWNER
            and self.actor.role is not MemberRole.OWNER
        )
        if removing_another_owner:
            raise PermissionDeniedError("Only an owner can remove another owner.")

        if target.role is MemberRole.OWNER:
            await self._require_another_owner_remains()

        await self.session.delete(target)
        await self.session.flush()

        logger.info(
            "Removed member",
            extra={
                "context": {
                    "organization_id": str(self.actor.organization_id),
                    "target_user_id": str(user_id),
                }
            },
        )

    # -- Guards ---------------------------------------------------------------

    def _require_administrator(self) -> None:
        if self.actor.role not in ADMINISTRATIVE_ROLES:
            raise PermissionDeniedError("You do not have permission to manage members.")

    async def _require_member(self, user_id: uuid.UUID) -> OrganizationMember:
        """Load a member of *this* organization.

        A user who exists but belongs elsewhere is reported as not found: the
        repository is scoped, so it cannot see them, and saying "forbidden"
        would confirm that the account exists.
        """
        member = await self.members.get_by_user(user_id)
        if member is None:
            raise NotFoundError("That user is not a member of this organization.")
        return member

    async def _require_another_owner_remains(self) -> None:
        """Refuse to leave the organization with no owner."""
        owners = await self.members.count_by_role(MemberRole.OWNER)
        if owners <= 1:
            raise ConflictError(
                "This is the organization's only owner. Promote another owner first."
            )
