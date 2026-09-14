"""Role-based access control on the membership endpoints."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, OrganizationMember
from app.models.enums import MemberRole, OrganizationStatus
from tests.integration.auth_helpers import add_member, build_organization, register

pytestmark = pytest.mark.integration

ORGANIZATION = "/api/v1/organization"
MEMBERS = "/api/v1/organization/members"


def member_url(user_id: uuid.UUID) -> str:
    return f"{MEMBERS}/{user_id}"


# -- Authentication is required -----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ORGANIZATION),
        ("GET", MEMBERS),
        ("PATCH", f"{MEMBERS}/{uuid.uuid4()}"),
        ("DELETE", f"{MEMBERS}/{uuid.uuid4()}"),
    ],
)
async def test_unauthenticated_requests_are_refused(
    method: str, path: str, api_client: AsyncClient
) -> None:
    response = await api_client.request(method, path, json={"role": "member"})

    assert response.status_code == 401


async def test_unauthenticated_is_401_not_403(api_client: AsyncClient) -> None:
    """A client must be able to tell "log in" from "you cannot do this"."""
    response = await api_client.get(MEMBERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# -- Member: ordinary use -----------------------------------------------------


async def test_a_member_can_read_their_organization(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]

    response = await api_client.get(ORGANIZATION, headers=member.headers())

    assert response.status_code == 200
    assert response.json()["id"] == str(org.id)


async def test_a_member_can_list_members(api_client: AsyncClient, session: AsyncSession) -> None:
    """Seeing your colleagues is ordinary use, not administration."""
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]

    response = await api_client.get(MEMBERS, headers=member.headers())

    assert response.status_code == 200
    assert {row["role"] for row in response.json()} == {"owner", "admin", "member"}


async def test_a_member_cannot_change_anyone_s_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.patch(
        member_url(admin.user_id), json={"role": "member"}, headers=member.headers()
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_a_member_cannot_promote_themselves(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The obvious attack, and it must fail on the role check."""
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]

    response = await api_client.patch(
        member_url(member.user_id), json={"role": "owner"}, headers=member.headers()
    )

    assert response.status_code == 403


async def test_a_member_cannot_remove_another_member(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.delete(member_url(admin.user_id), headers=member.headers())

    assert response.status_code == 403


async def test_a_member_may_remove_themselves(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Leaving an organization is not an administrative act."""
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]

    response = await api_client.delete(member_url(member.user_id), headers=member.headers())

    assert response.status_code == 204
    remaining = await api_client.get(MEMBERS, headers=org.owner.headers())
    assert str(member.user_id) not in remaining.text


# -- Admin: manages members, but is not an owner ------------------------------


async def test_an_admin_can_change_a_member_s_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]
    member = org.members[MemberRole.MEMBER]

    response = await api_client.patch(
        member_url(member.user_id), json={"role": "admin"}, headers=admin.headers()
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_an_admin_can_remove_a_member(api_client: AsyncClient, session: AsyncSession) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]
    member = org.members[MemberRole.MEMBER]

    response = await api_client.delete(member_url(member.user_id), headers=admin.headers())

    assert response.status_code == 204


async def test_an_admin_cannot_grant_the_owner_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Otherwise an admin could promote a second account they control and take
    over the organization."""
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]
    member = org.members[MemberRole.MEMBER]

    response = await api_client.patch(
        member_url(member.user_id), json={"role": "owner"}, headers=admin.headers()
    )

    assert response.status_code == 403


async def test_an_admin_cannot_demote_an_owner(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.patch(
        member_url(org.owner.user_id), json={"role": "member"}, headers=admin.headers()
    )

    assert response.status_code == 403


async def test_an_admin_cannot_remove_an_owner(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.delete(member_url(org.owner.user_id), headers=admin.headers())

    assert response.status_code == 403


async def test_an_admin_cannot_change_their_own_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.patch(
        member_url(admin.user_id), json={"role": "owner"}, headers=admin.headers()
    )

    assert response.status_code == 403


# -- Owner: full administration -----------------------------------------------


async def test_an_owner_can_promote_a_member_to_admin(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]

    response = await api_client.patch(
        member_url(member.user_id), json={"role": "admin"}, headers=org.owner.headers()
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_an_owner_can_grant_the_owner_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.patch(
        member_url(admin.user_id), json={"role": "owner"}, headers=org.owner.headers()
    )

    assert response.status_code == 200
    assert response.json()["role"] == "owner"


async def test_an_owner_can_remove_an_admin(api_client: AsyncClient, session: AsyncSession) -> None:
    org = await build_organization(api_client, session)
    admin = org.members[MemberRole.ADMIN]

    response = await api_client.delete(member_url(admin.user_id), headers=org.owner.headers())

    assert response.status_code == 204


async def test_an_owner_cannot_change_their_own_role(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Self-demotion is almost always a mistake, and the last-owner rule would
    not catch it when another owner exists."""
    org = await build_organization(api_client, session)

    response = await api_client.patch(
        member_url(org.owner.user_id), json={"role": "member"}, headers=org.owner.headers()
    )

    assert response.status_code == 403


# -- The last owner -----------------------------------------------------------


async def test_the_only_owner_cannot_be_demoted(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """An organization with no owner could never be administered again."""
    org = await build_organization(api_client, session)
    second_owner = await add_member(api_client, session, org.id, MemberRole.OWNER)

    # The second owner demoting the first is fine: two owners exist.
    demote_first = await api_client.patch(
        member_url(org.owner.user_id),
        json={"role": "member"},
        headers=second_owner.headers(),
    )
    assert demote_first.status_code == 200

    # Now only one owner remains, and nobody can demote them.
    admin = await add_member(api_client, session, org.id, MemberRole.ADMIN)
    demote_last = await api_client.patch(
        member_url(second_owner.user_id), json={"role": "member"}, headers=admin.headers()
    )

    assert demote_last.status_code in (403, 409)


async def test_the_only_owner_cannot_remove_themselves(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The accidental self-removal the requirement calls out."""
    org = await build_organization(api_client, session)

    response = await api_client.delete(member_url(org.owner.user_id), headers=org.owner.headers())

    assert response.status_code == 409
    assert "owner" in response.json()["error"]["message"].lower()


async def test_an_owner_may_leave_once_another_owner_exists(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    await add_member(api_client, session, org.id, MemberRole.OWNER)

    response = await api_client.delete(member_url(org.owner.user_id), headers=org.owner.headers())

    assert response.status_code == 204


# -- Unknown targets ----------------------------------------------------------


async def test_changing_a_non_member_s_role_is_not_found(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)

    response = await api_client.patch(
        member_url(uuid.uuid4()), json={"role": "admin"}, headers=org.owner.headers()
    )

    assert response.status_code == 404


async def test_a_user_with_no_organization_is_refused(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Authenticated, but with nothing to act on.

    Not reachable through the API - a user is the sole owner of the
    organization registration created for them, so they cannot leave it - but
    an administrator could remove the row, and the request must then be
    refused rather than fall through to some default tenant.
    """
    from sqlalchemy import delete

    from app.models.organization import OrganizationMember

    account = await register(api_client)
    await session.execute(
        delete(OrganizationMember).where(OrganizationMember.user_id == account.user_id)
    )
    await session.flush()

    response = await api_client.get(MEMBERS, headers=account.headers(include_organization=False))

    assert response.status_code == 403


# -- Organization lifecycle ---------------------------------------------------


@pytest.mark.parametrize("status", [OrganizationStatus.SUSPENDED, OrganizationStatus.ARCHIVED])
async def test_an_inactive_organization_cannot_be_acted_on(
    status: OrganizationStatus, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Suspending an organization has to stop its members working in it.

    The user's status and the membership's status were already enforced; this
    is the third, and without it the column would be decorative.
    """
    org = await build_organization(api_client, session)
    record = await session.get(Organization, org.id)
    assert record is not None
    record.status = status
    await session.flush()

    for account in (org.owner, org.members[MemberRole.ADMIN], org.members[MemberRole.MEMBER]):
        response = await api_client.get(ORGANIZATION, headers=account.headers())
        assert response.status_code == 403, account.role
        assert response.json()["error"]["code"] == "permission_denied"


async def test_an_inactive_organization_blocks_membership_administration(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    org = await build_organization(api_client, session)
    member = org.members[MemberRole.MEMBER]
    record = await session.get(Organization, org.id)
    assert record is not None
    record.status = OrganizationStatus.SUSPENDED
    await session.flush()

    listed = await api_client.get(MEMBERS, headers=org.owner.headers())
    changed = await api_client.patch(
        member_url(member.user_id), json={"role": "admin"}, headers=org.owner.headers()
    )
    removed = await api_client.delete(member_url(member.user_id), headers=org.owner.headers())

    assert listed.status_code == changed.status_code == removed.status_code == 403


async def test_a_suspended_organization_is_indistinguishable_from_a_foreign_one(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Otherwise the refusal would confirm that the organization exists."""
    org_a = await build_organization(api_client, session)
    org_b = await build_organization(api_client, session)
    record = await session.get(Organization, org_b.id)
    assert record is not None
    record.status = OrganizationStatus.SUSPENDED
    await session.flush()

    # org_a's owner naming a suspended organization they are not in, versus
    # naming one that does not exist at all.
    suspended = await api_client.get(ORGANIZATION, headers=org_a.owner.headers(org_b.id))
    absent = await api_client.get(ORGANIZATION, headers=org_a.owner.headers(uuid.uuid4()))

    assert suspended.status_code == absent.status_code == 403
    assert suspended.json()["error"] == absent.json()["error"]


async def test_the_only_membership_being_in_a_suspended_organization_is_refused(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The fallback path must not select an organization that is not usable."""
    account = await register(api_client)
    record = await session.get(Organization, account.organization_id)
    assert record is not None
    record.status = OrganizationStatus.SUSPENDED
    await session.flush()

    response = await api_client.get(
        ORGANIZATION, headers=account.headers(include_organization=False)
    )

    assert response.status_code == 403


async def test_an_active_organization_is_still_reachable_alongside_a_suspended_one(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Suspending one tenant must not lock a member out of another."""
    org_a = await build_organization(api_client, session)
    org_b = await build_organization(api_client, session)
    consultant = await add_member(api_client, session, org_b.id, MemberRole.MEMBER)
    session.add(
        OrganizationMember(
            organization_id=org_a.id, user_id=consultant.user_id, role=MemberRole.MEMBER
        )
    )
    record = await session.get(Organization, org_b.id)
    assert record is not None
    record.status = OrganizationStatus.SUSPENDED
    await session.flush()

    blocked = await api_client.get(ORGANIZATION, headers=consultant.headers(org_b.id))
    allowed = await api_client.get(ORGANIZATION, headers=consultant.headers(org_a.id))

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["id"] == str(org_a.id)
