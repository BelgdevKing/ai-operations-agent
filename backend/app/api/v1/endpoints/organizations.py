"""Organization and membership endpoints.

Every route acts on *the current organization*, resolved from the caller's
verified membership. No route takes an organization id in its path, query or
body: there is nothing for a client to tamper with. Callers who belong to more
than one organization choose between them with the ``X-Organization-ID``
header, which is checked against their memberships before anything else runs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import (
    CurrentMembership,
    CurrentOrganization,
    MembershipServiceDep,
    RequireAdmin,
)
from app.schemas.common import ErrorResponse
from app.schemas.organization import (
    MemberResponse,
    MemberRoleUpdate,
    OrganizationResponse,
)

router = APIRouter()

AUTH_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "Authenticated, but not permitted"},
}


@router.get(
    "",
    response_model=OrganizationResponse,
    summary="The organization this request acts on",
    responses=AUTH_RESPONSES,
)
async def read_organization(organization: CurrentOrganization) -> OrganizationResponse:
    """Return the current organization.

    Which organization that is comes from the caller's membership, so this can
    only ever return one they belong to.
    """
    return OrganizationResponse.model_validate(organization)


@router.get(
    "/members",
    response_model=list[MemberResponse],
    summary="List the organization's members",
    responses=AUTH_RESPONSES,
)
async def list_members(
    service: MembershipServiceDep,
    _membership: CurrentMembership,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MemberResponse]:
    """List members of the current organization.

    Open to any active member: seeing who your colleagues are is ordinary use,
    not administration. Changing anything about them is not.
    """
    members = await service.list_members(limit=limit, offset=offset)
    return [MemberResponse.model_validate(member) for member in members]


@router.patch(
    "/members/{user_id}",
    response_model=MemberResponse,
    summary="Change a member's role",
    responses={
        **AUTH_RESPONSES,
        404: {"model": ErrorResponse, "description": "Not a member of this organization"},
        409: {"model": ErrorResponse, "description": "Would leave the organization with no owner"},
    },
)
async def change_member_role(
    user_id: uuid.UUID,
    payload: MemberRoleUpdate,
    service: MembershipServiceDep,
    _admin: RequireAdmin,
) -> MemberResponse:
    """Change one member's role.

    Requires admin or owner. Beyond that: only an owner may grant or revoke the
    owner role, nobody may change their own role, and the last owner cannot be
    demoted.

    A ``user_id`` belonging to another organization returns 404 - the lookup is
    tenant-scoped and simply cannot see them.
    """
    member = await service.change_role(user_id, payload.role)
    return MemberResponse.model_validate(member)


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
    responses={
        **AUTH_RESPONSES,
        404: {"model": ErrorResponse, "description": "Not a member of this organization"},
        409: {"model": ErrorResponse, "description": "Would leave the organization with no owner"},
    },
)
async def remove_member(
    user_id: uuid.UUID,
    service: MembershipServiceDep,
    _membership: CurrentMembership,
) -> None:
    """Remove a member from the organization.

    Requires admin or owner to remove somebody else; any member may remove
    themselves. Only an owner may remove another owner, and the last owner
    cannot be removed at all.

    Authorization is enforced in the service rather than by a route dependency,
    because whether this is an administrative act depends on whether the target
    is the caller.
    """
    await service.remove_member(user_id)
