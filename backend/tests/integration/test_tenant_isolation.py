"""Tenant isolation.

Two organizations, A and B, each with their own members and their own
tenant-owned resources. Nothing belonging to B may be reachable by anyone in A,
whatever the request says.

Covered at both levels the guarantee rests on:

* the API, where the organization a request acts on comes from the caller's
  verified membership rather than from the request;
* the repository, where every query for a tenant-owned model carries the
  organization filter.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Agent,
    Conversation,
    Document,
    Organization,
    OrganizationMember,
    User,
    Workflow,
)
from app.models.enums import MemberRole
from app.repositories.tenant import TenantScopedRepository
from tests.integration.auth_helpers import Organization as Org
from tests.integration.auth_helpers import add_member, build_organization, register

pytestmark = pytest.mark.integration

ORGANIZATION = "/api/v1/organization"
MEMBERS = "/api/v1/organization/members"


# -- Repositories used only by these tests, to prove the pattern generalises ---


class AgentRepository(TenantScopedRepository[Agent]):
    model = Agent


class DocumentRepository(TenantScopedRepository[Document]):
    model = Document


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation


class WorkflowRepository(TenantScopedRepository[Workflow]):
    model = Workflow


@pytest.fixture
async def two_organizations(api_client: AsyncClient, session: AsyncSession) -> tuple[Org, Org]:
    """Organization A and Organization B, with no member in common."""
    org_a = await build_organization(api_client, session)
    org_b = await build_organization(api_client, session)
    return org_a, org_b


# -- API: the organization comes from membership, not from the request --------


async def test_a_caller_reads_only_their_own_organization(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations

    response = await api_client.get(ORGANIZATION, headers=org_a.owner.headers())

    assert response.status_code == 200
    assert response.json()["id"] == str(org_a.id)
    assert response.json()["id"] != str(org_b.id)


async def test_naming_another_organization_in_the_header_is_refused(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    """The header is a request, not a grant."""
    org_a, org_b = two_organizations

    response = await api_client.get(ORGANIZATION, headers=org_a.owner.headers(org_b.id))

    assert response.status_code == 403
    assert str(org_b.id) not in response.text


async def test_member_list_never_includes_another_organization(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations

    response = await api_client.get(MEMBERS, headers=org_a.owner.headers())

    assert response.status_code == 200
    returned = {row["user"]["id"] for row in response.json()}
    assert str(org_a.owner.user_id) in returned
    for account in (org_b.owner, *org_b.members.values()):
        assert str(account.user_id) not in returned


async def test_an_owner_of_a_cannot_list_b_s_members(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations

    response = await api_client.get(MEMBERS, headers=org_a.owner.headers(org_b.id))

    assert response.status_code == 403


async def test_an_owner_of_a_cannot_change_a_role_in_b(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    """Owner in their own tenant is not owner anywhere else."""
    org_a, org_b = two_organizations
    victim = org_b.members[MemberRole.MEMBER]

    response = await api_client.patch(
        f"{MEMBERS}/{victim.user_id}",
        json={"role": "owner"},
        headers=org_a.owner.headers(org_b.id),
    )

    assert response.status_code == 403


async def test_an_owner_of_a_cannot_remove_a_member_of_b(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations
    victim = org_b.members[MemberRole.MEMBER]

    response = await api_client.delete(
        f"{MEMBERS}/{victim.user_id}", headers=org_a.owner.headers(org_b.id)
    )

    assert response.status_code == 403


async def test_targeting_a_foreign_user_within_your_own_organization_is_not_found(
    api_client: AsyncClient, session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """The subtler attack: a valid organization header, but a user id from
    another tenant. The scoped lookup cannot see them, so it is a 404 - and
    crucially not a role change applied across tenants."""
    org_a, org_b = two_organizations
    victim = org_b.members[MemberRole.MEMBER]

    response = await api_client.patch(
        f"{MEMBERS}/{victim.user_id}",
        json={"role": "member"},
        headers=org_a.owner.headers(),
    )

    assert response.status_code == 404

    membership = (
        await session.execute(
            select(OrganizationMember)
            .where(OrganizationMember.user_id == victim.user_id)
            .where(OrganizationMember.organization_id == org_b.id)
        )
    ).scalar_one()
    assert membership.role is MemberRole.MEMBER


async def test_removing_a_foreign_user_within_your_own_organization_is_not_found(
    api_client: AsyncClient, session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations
    victim = org_b.members[MemberRole.MEMBER]

    response = await api_client.delete(f"{MEMBERS}/{victim.user_id}", headers=org_a.owner.headers())

    assert response.status_code == 404
    still_there = (
        await session.execute(
            select(OrganizationMember)
            .where(OrganizationMember.user_id == victim.user_id)
            .where(OrganizationMember.organization_id == org_b.id)
        )
    ).scalar_one_or_none()
    assert still_there is not None


async def test_a_nonexistent_organization_is_refused_like_a_foreign_one(
    api_client: AsyncClient, two_organizations: tuple[Org, Org]
) -> None:
    """Identical responses, so this cannot be used to discover which
    organizations exist."""
    org_a, org_b = two_organizations

    foreign = await api_client.get(ORGANIZATION, headers=org_a.owner.headers(org_b.id))
    absent = await api_client.get(ORGANIZATION, headers=org_a.owner.headers(uuid.uuid4()))

    assert foreign.status_code == absent.status_code == 403
    assert foreign.json()["error"] == absent.json()["error"]


async def test_a_user_in_both_organizations_gets_only_the_one_they_name(
    api_client: AsyncClient, session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """Legitimate multi-tenancy: the same person, two tenants, no bleed."""
    org_a, org_b = two_organizations
    consultant = await add_member(api_client, session, org_a.id, MemberRole.ADMIN)
    session.add(
        OrganizationMember(
            organization_id=org_b.id,
            user_id=consultant.user_id,
            role=MemberRole.MEMBER,
        )
    )
    await session.flush()

    in_a = await api_client.get(ORGANIZATION, headers=consultant.headers(org_a.id))
    in_b = await api_client.get(ORGANIZATION, headers=consultant.headers(org_b.id))

    assert in_a.json()["id"] == str(org_a.id)
    assert in_b.json()["id"] == str(org_b.id)

    # And the role differs per organization, because it lives on the membership.
    members_of_a = await api_client.get(MEMBERS, headers=consultant.headers(org_a.id))
    assert members_of_a.status_code == 200
    change_in_b = await api_client.patch(
        f"{MEMBERS}/{org_b.members[MemberRole.MEMBER].user_id}",
        json={"role": "admin"},
        headers=consultant.headers(org_b.id),
    )
    assert change_in_b.status_code == 403


async def test_an_ambiguous_request_is_refused_rather_than_guessed(
    api_client: AsyncClient, session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """Belonging to several organizations and naming none must not silently
    pick one."""
    org_a, _ = two_organizations
    consultant = await add_member(api_client, session, org_a.id, MemberRole.MEMBER)

    response = await api_client.get(
        ORGANIZATION, headers=consultant.headers(include_organization=False)
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "organization_required"


async def test_a_single_membership_needs_no_header(api_client: AsyncClient) -> None:
    """The common case stays simple."""
    account = await register(api_client)

    response = await api_client.get(
        ORGANIZATION, headers=account.headers(include_organization=False)
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(account.organization_id)


# -- Repository layer: the filter every tenant-owned query carries ------------


@pytest.fixture
async def tenant_resources(
    session: AsyncSession, two_organizations: tuple[Org, Org]
) -> tuple[Org, Org]:
    """One agent, document, conversation and workflow in each organization."""
    org_a, org_b = two_organizations

    for org, owner_id in ((org_a, org_a.owner.user_id), (org_b, org_b.owner.user_id)):
        agent = Agent(organization_id=org.id, name="Support", model="claude-sonnet-5")
        workflow = Workflow(organization_id=org.id, name="Refunds")
        document = Document(
            organization_id=org.id,
            filename="policy.pdf",
            content_type="application/pdf",
            storage_key=f"docs/{uuid.uuid4()}",
            size_bytes=1024,
        )
        session.add_all([agent, workflow, document])
        await session.flush()
        session.add(Conversation(organization_id=org.id, user_id=owner_id, agent_id=agent.id))
    await session.flush()

    return org_a, org_b


@pytest.mark.parametrize(
    "repository_class",
    [AgentRepository, DocumentRepository, ConversationRepository, WorkflowRepository],
)
async def test_a_scoped_repository_lists_only_its_own_tenant(
    repository_class: type[TenantScopedRepository[Agent]],
    session: AsyncSession,
    tenant_resources: tuple[Org, Org],
) -> None:
    """Four different tenant-owned models, one filter."""
    org_a, org_b = tenant_resources

    rows_a = await repository_class(session, org_a.id).list()
    rows_b = await repository_class(session, org_b.id).list()

    assert len(rows_a) == 1
    assert len(rows_b) == 1
    assert rows_a[0].organization_id == org_a.id
    assert rows_b[0].organization_id == org_b.id
    assert rows_a[0].id != rows_b[0].id


@pytest.mark.parametrize(
    "repository_class",
    [AgentRepository, DocumentRepository, ConversationRepository, WorkflowRepository],
)
async def test_a_scoped_repository_cannot_fetch_another_tenant_s_row_by_id(
    repository_class: type[TenantScopedRepository[Agent]],
    session: AsyncSession,
    tenant_resources: tuple[Org, Org],
) -> None:
    """Knowing the primary key is not enough - the filter still applies."""
    org_a, org_b = tenant_resources

    foreign = (await repository_class(session, org_b.id).list())[0]
    fetched = await repository_class(session, org_a.id).get(foreign.id)

    assert fetched is None


@pytest.mark.parametrize(
    "repository_class",
    [AgentRepository, DocumentRepository, ConversationRepository, WorkflowRepository],
)
async def test_a_scoped_repository_cannot_delete_another_tenant_s_row(
    repository_class: type[TenantScopedRepository[Agent]],
    session: AsyncSession,
    tenant_resources: tuple[Org, Org],
) -> None:
    org_a, org_b = tenant_resources
    foreign = (await repository_class(session, org_b.id).list())[0]

    deleted = await repository_class(session, org_a.id).delete(foreign.id)
    await session.flush()

    assert deleted is False
    assert await repository_class(session, org_b.id).get(foreign.id) is not None


async def test_a_scoped_repository_counts_only_its_own_tenant(
    session: AsyncSession, tenant_resources: tuple[Org, Org]
) -> None:
    org_a, org_b = tenant_resources

    assert await AgentRepository(session, org_a.id).count() == 1
    assert await AgentRepository(session, org_b.id).count() == 1


async def test_adding_through_a_scoped_repository_stamps_the_owner(
    session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """A caller cannot create a row owned by someone else, even deliberately."""
    org_a, org_b = two_organizations
    repository = AgentRepository(session, org_a.id)

    # organization_id is set to B, and must be overwritten with A.
    agent = Agent(organization_id=org_b.id, name="Smuggled", model="claude-sonnet-5")
    repository.add(agent)
    await session.flush()

    assert agent.organization_id == org_a.id
    assert await AgentRepository(session, org_b.id).get(agent.id) is None


async def test_membership_queries_are_scoped(
    session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """The repository behind the members endpoint, checked directly."""
    from app.repositories.membership import MembershipRepository

    org_a, org_b = two_organizations
    repository = MembershipRepository(session, org_a.id)

    members = await repository.list_with_users()
    assert {member.organization_id for member in members} == {org_a.id}

    foreign_user = org_b.owner.user_id
    assert await repository.get_by_user(foreign_user) is None


async def test_the_owner_count_is_per_organization(
    session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """The last-owner guard must not be satisfied by another tenant's owners."""
    from app.repositories.membership import MembershipRepository

    org_a, org_b = two_organizations

    assert await MembershipRepository(session, org_a.id).count_by_role(MemberRole.OWNER) == 1
    assert await MembershipRepository(session, org_b.id).count_by_role(MemberRole.OWNER) == 1


async def test_users_are_global_but_their_data_is_not(
    api_client: AsyncClient, session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    """Users are deliberately not tenant-owned; what they can reach is."""
    org_a, org_b = two_organizations

    assert await session.get(User, org_b.owner.user_id) is not None

    response = await api_client.get("/api/v1/auth/me", headers=org_a.owner.headers())
    organizations = {row["organization"]["id"] for row in response.json()["memberships"]}

    assert organizations == {str(org_a.id)}


async def test_organizations_remain_separate_rows(
    session: AsyncSession, two_organizations: tuple[Org, Org]
) -> None:
    org_a, org_b = two_organizations

    a = await session.get(Organization, org_a.id)
    b = await session.get(Organization, org_b.id)

    assert a is not None and b is not None
    assert a.id != b.id
    assert a.slug != b.slug
