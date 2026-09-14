"""Shared FastAPI dependencies.

Annotated aliases keep endpoint signatures short and make the wiring explicit
in one place instead of being repeated across routers.

The authorization chain, in order:

``CurrentUser``
    Who is calling, from a verified access token.
``CurrentMembership``
    Which organization they are acting in, and their role in it. Chosen by the
    client but *verified* against the database on every request - a header
    naming an organization the caller does not belong to is refused, not
    honoured.
``CurrentOrganization``
    The organization itself, derived from the membership.
``require_role(...)``
    A dependency factory refusing callers whose role is not sufficient.

Membership and role are read from the database each time rather than carried in
the token, so revoking a membership or reducing a role takes effect on the next
request instead of when the token happens to expire.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, ClassVar

from fastapi import Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.core.exceptions import AppError, PermissionDeniedError, UnauthorizedError
from app.core.security import TokenError, decode_access_token
from app.models.enums import MemberRole, OrganizationStatus, UserStatus
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.repositories.membership import MembershipLookup
from app.repositories.user import UserRepository
from app.services.auth import AuthService
from app.services.health import HealthService
from app.services.membership import MembershipService

ORGANIZATION_HEADER = "X-Organization-ID"

# auto_error=False so a missing header raises our own 401 in the standard error
# envelope rather than Starlette's bare {"detail": ...}.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Access token from POST /api/v1/auth/login, sent as: Authorization: Bearer <token>",
)


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running application was built with.

    Read from application state rather than the process-wide loader, so an
    application constructed with explicit settings - as tests do - behaves
    consistently everywhere, including inside endpoints.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
"""Settings of the running application."""

SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""Database session scoped to the request; commits on success, rolls back on error."""


def get_health_service(settings: SettingsDep) -> HealthService:
    return HealthService(settings)


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
"""Health and readiness reporting."""


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
"""Registration and login."""


# -- Authentication -----------------------------------------------------------


class InvalidCredentials(UnauthorizedError):
    """401 carrying the WWW-Authenticate header the Bearer scheme requires."""

    headers: ClassVar[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Resolve the caller from their access token.

    Every rejection - absent, malformed, expired, wrongly signed, or naming a
    user who no longer exists or is no longer active - produces the same 401.
    """
    if credentials is None or not credentials.credentials:
        raise InvalidCredentials("Not authenticated.")

    try:
        claims = decode_access_token(credentials.credentials, settings)
    except TokenError as exc:
        raise InvalidCredentials("Could not validate credentials.") from exc

    user = await UserRepository(session).get(claims.user_id)

    # A token outliving its user, or issued before the account was suspended,
    # must stop working immediately - which is why this is checked per request.
    if user is None or user.status is not UserStatus.ACTIVE:
        raise InvalidCredentials("Could not validate credentials.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
"""The authenticated user."""


# -- Tenant context -----------------------------------------------------------


class OrganizationRequired(AppError):
    """The caller belongs to several organizations and named none of them."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "organization_required"
    message = (
        f"You belong to more than one organization. Send the {ORGANIZATION_HEADER} "
        "header to choose which one this request acts on."
    )


async def get_current_membership(
    session: SessionDep,
    user: CurrentUser,
    organization_id: Annotated[
        uuid.UUID | None,
        Header(
            alias=ORGANIZATION_HEADER,
            description=(
                "Organization this request acts on. Required only when you belong to "
                "more than one. Always checked against your membership."
            ),
        ),
    ] = None,
) -> OrganizationMember:
    """Resolve which organization the caller is acting in, and their role.

    The header is a *request*, not a grant. It is resolved against the caller's
    own active memberships, so naming another tenant's id yields a 403 rather
    than access. When it is absent and the caller belongs to exactly one
    organization, that one is used.

    Three things must hold, and all three are checked against the database on
    every request: the user is active (``get_current_user``), the membership is
    active (the lookup filters on it), and the organization itself is active.
    """
    lookup = MembershipLookup(session)

    if organization_id is not None:
        membership = await lookup.get_for_user_and_organization(user.id, organization_id)
        # Deliberately identical whether the organization does not exist, exists
        # and the caller is not in it, or exists and is suspended: otherwise
        # this endpoint would confirm the existence and state of other tenants.
        if membership is None or not _organization_is_usable(membership):
            raise PermissionDeniedError("You do not have access to that organization.")
        return membership

    memberships = [m for m in await lookup.list_for_user(user.id) if _organization_is_usable(m)]
    if not memberships:
        raise PermissionDeniedError("You do not have access to an active organization.")
    if len(memberships) > 1:
        raise OrganizationRequired()

    return memberships[0]


def _organization_is_usable(membership: OrganizationMember) -> bool:
    """Whether the organization behind a membership may still be acted on.

    Suspending or archiving an organization has to stop its members working in
    it. Without this the status column would be decorative: the user's status
    and the membership's status are both enforced, and an organization that had
    been suspended for abuse or non-payment would carry on regardless.
    """
    return membership.organization.status is OrganizationStatus.ACTIVE


CurrentMembership = Annotated[OrganizationMember, Depends(get_current_membership)]
"""The caller's verified membership of the organization this request acts on."""


async def get_current_organization(membership: CurrentMembership) -> Organization:
    """The organization this request acts on.

    Derived from the verified membership, never from the request directly.
    """
    return membership.organization


CurrentOrganization = Annotated[Organization, Depends(get_current_organization)]
"""The organization this request acts on."""


async def get_current_organization_id(membership: CurrentMembership) -> uuid.UUID:
    """The tenant id to scope repositories with."""
    return membership.organization_id


CurrentOrganizationId = Annotated[uuid.UUID, Depends(get_current_organization_id)]
"""Tenant id for constructing a TenantScopedRepository."""


# -- Authorization ------------------------------------------------------------

# Ordered from least to most privileged. A role satisfies a requirement if it
# appears at or above the required rank, so require_role(ADMIN) also admits an
# owner without every call site having to list both.
ROLE_RANK: dict[MemberRole, int] = {
    MemberRole.MEMBER: 0,
    MemberRole.ADMIN: 1,
    MemberRole.OWNER: 2,
}


RoleDependency = Callable[..., Awaitable[OrganizationMember]]


def require_role(minimum: MemberRole) -> RoleDependency:
    """Build a dependency admitting only roles at or above *minimum*.

        @router.delete("/members/{user_id}", dependencies=[Depends(require_role(MemberRole.ADMIN))])

    Authentication failures stay 401; a valid caller whose role is too low gets
    403, so a client can tell "log in" from "you cannot do this".
    """

    async def dependency(membership: CurrentMembership) -> OrganizationMember:
        if ROLE_RANK[membership.role] < ROLE_RANK[minimum]:
            raise PermissionDeniedError(f"This action requires the {minimum.value} role or higher.")
        return membership

    return dependency


RequireOwner = Annotated[OrganizationMember, Depends(require_role(MemberRole.OWNER))]
"""Owner-only operations."""

RequireAdmin = Annotated[OrganizationMember, Depends(require_role(MemberRole.ADMIN))]
"""Administrative operations; owners qualify too."""

RequireMember = Annotated[OrganizationMember, Depends(require_role(MemberRole.MEMBER))]
"""Any active member of the organization."""


def get_membership_service(session: SessionDep, membership: CurrentMembership) -> MembershipService:
    """Membership administration, fixed to the caller's organization."""
    return MembershipService(session, membership)


MembershipServiceDep = Annotated[MembershipService, Depends(get_membership_service)]
"""Membership administration for the current organization."""
