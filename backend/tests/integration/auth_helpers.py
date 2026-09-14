"""Helpers for exercising the authenticated API.

Accounts are created through the real endpoints rather than by inserting rows,
so registration, hashing and token issue are on the path of every test that
needs a logged-in caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ORGANIZATION_HEADER
from app.models.enums import MemberRole, MembershipStatus
from app.models.organization import OrganizationMember

PASSWORD = "a-perfectly-fine-password"


@dataclass
class Account:
    """A registered user, their organization, and a usable token."""

    user_id: uuid.UUID
    email: str
    organization_id: uuid.UUID
    organization_slug: str
    role: MemberRole
    token: str

    def headers(
        self,
        organization_id: uuid.UUID | None = None,
        *,
        include_organization: bool = True,
    ) -> dict[str, str]:
        """Authorization header, naming the organization to act on.

        Registration always creates an organization, so an account added to a
        second one belongs to two - and a request that names neither is
        genuinely ambiguous. Tests therefore state which organization they mean,
        as a real multi-organization client would.

        Passing ``organization_id`` is how a test attempts to act on a tenant it
        may not belong to; ``include_organization=False`` exercises the
        single-membership fallback.
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        if include_organization:
            headers[ORGANIZATION_HEADER] = str(organization_id or self.organization_id)
        elif organization_id is not None:
            headers[ORGANIZATION_HEADER] = str(organization_id)
        return headers


@dataclass
class Organization:
    """An organization with its owner and any extra members."""

    id: uuid.UUID
    owner: Account
    members: dict[MemberRole, Account] = field(default_factory=dict)


async def register(
    client: AsyncClient,
    *,
    email: str | None = None,
    password: str = PASSWORD,
    organization_name: str | None = None,
) -> Account:
    """Register through the API and return the resulting account."""
    unique = uuid.uuid4().hex[:10]
    payload = {
        "email": email or f"user-{unique}@example.com",
        "password": password,
        "organization_name": organization_name or f"Org {unique}",
        "first_name": "Test",
        "last_name": "User",
    }

    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()

    return Account(
        user_id=uuid.UUID(body["user"]["id"]),
        email=body["user"]["email"],
        organization_id=uuid.UUID(body["organization"]["id"]),
        organization_slug=body["organization"]["slug"],
        role=MemberRole(body["role"]),
        token=body["token"]["access_token"],
    )


async def login(client: AsyncClient, email: str, password: str = PASSWORD) -> str:
    """Log in and return the access token."""
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def add_member(
    client: AsyncClient,
    session: AsyncSession,
    organization_id: uuid.UUID,
    role: MemberRole,
) -> Account:
    """Register a new account and join it to an existing organization.

    Registration always creates its own organization and makes the user its
    owner, so the extra membership is inserted directly. There is no invitation
    endpoint yet - that is deliberately out of scope for this phase.
    """
    account = await register(client)

    session.add(
        OrganizationMember(
            organization_id=organization_id,
            user_id=account.user_id,
            role=role,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()

    return Account(
        user_id=account.user_id,
        email=account.email,
        organization_id=organization_id,
        organization_slug=account.organization_slug,
        role=role,
        token=account.token,
    )


async def build_organization(
    client: AsyncClient,
    session: AsyncSession,
    *,
    roles: tuple[MemberRole, ...] = (MemberRole.ADMIN, MemberRole.MEMBER),
) -> Organization:
    """An organization with an owner plus one account per requested role."""
    owner = await register(client)
    organization = Organization(id=owner.organization_id, owner=owner)

    for role in roles:
        organization.members[role] = await add_member(client, session, owner.organization_id, role)

    return organization
