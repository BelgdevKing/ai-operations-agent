"""Managing workflow definitions: drafting, validating, activating, versioning.

Against real PostgreSQL and through the real endpoints, so authentication,
membership and the role requirements are on the path of every test.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.workflows import (
    SHIPMENT_EXCEPTION_DESCRIPTION,
    SHIPMENT_EXCEPTION_WORKFLOW,
    shipment_exception_definition,
)
from app.models.enums import MemberRole, WorkflowStatus
from app.models.workflow import Workflow
from tests.integration.auth_helpers import add_member, register
from tests.integration.workflow_helpers import (
    WORKFLOWS_URL,
    activate,
    create_workflow,
    lookup_workflow,
    published,
    tool_step,
)

pytestmark = pytest.mark.integration


# -- Drafting -----------------------------------------------------------------


async def test_a_draft_is_created_and_is_not_runnable(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)

    created = await create_workflow(api_client, account.headers(), lookup_workflow())

    assert created["status"] == "draft"
    assert created["version"] == 1

    record = await session.get(Workflow, uuid.UUID(created["id"]))
    assert record is not None
    assert record.status is WorkflowStatus.DRAFT
    assert record.organization_id == account.organization_id


async def test_a_draft_cannot_be_started(api_client: AsyncClient) -> None:
    """Starting a draft would be the platform deciding somebody had finished."""
    account = await register(api_client)
    created = await create_workflow(api_client, account.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/runs", json={"input": {}}, headers=account.headers()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_not_active"


async def test_a_malformed_definition_is_refused_when_it_is_written(
    api_client: AsyncClient,
) -> None:
    """Not stored and discovered at activation: refused with the reason now."""
    account = await register(api_client)

    response = await api_client.post(
        WORKFLOWS_URL,
        json={
            "name": "Broken",
            "definition": {"entry": "a", "steps": [{"id": "a", "type": "shell", "next": None}]},
        },
        headers=account.headers(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_invalid"


async def test_only_an_administrator_may_draft_a_workflow(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Writing a definition decides which tools this organization's automation
    may call and on whose behalf. That is administrative."""
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)

    response = await api_client.post(
        WORKFLOWS_URL,
        json={"name": "Theirs", "definition": lookup_workflow()},
        headers=member.headers(),
    )

    assert response.status_code == 403


# -- Activation ---------------------------------------------------------------


async def test_activation_validates_against_the_world(api_client: AsyncClient) -> None:
    account = await register(api_client)
    created = await create_workflow(
        api_client,
        account.headers(),
        {"entry": "a", "steps": [tool_step("a", "not_installed")]},
    )

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/activate", headers=account.headers()
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "workflow_invalid"
    assert any("not a known tool" in problem for problem in body["details"]["problems"])


async def test_a_refused_activation_leaves_the_version_a_draft(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """So nothing can start it."""
    account = await register(api_client)
    created = await create_workflow(
        api_client,
        account.headers(),
        {"entry": "a", "steps": [tool_step("a", "not_installed")]},
    )

    await api_client.post(f"{WORKFLOWS_URL}/{created['id']}/activate", headers=account.headers())

    record = await session.get(Workflow, uuid.UUID(created["id"]))
    assert record is not None
    await session.refresh(record)
    assert record.status is WorkflowStatus.DRAFT


async def test_a_cyclic_definition_cannot_be_activated(api_client: AsyncClient) -> None:
    account = await register(api_client)
    created = await create_workflow(
        api_client,
        account.headers(),
        {
            "entry": "a",
            "steps": [
                tool_step("a", "get_shipment", next_step="b"),
                tool_step("b", "get_shipment", next_step="a"),
            ],
        },
    )

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/activate", headers=account.headers()
    )

    assert response.status_code == 422
    assert any("cycle" in problem for problem in response.json()["error"]["details"]["problems"])


async def test_the_demonstration_workflow_activates(api_client: AsyncClient) -> None:
    """The definition in `app/demo/workflows.py` passes every rule."""
    account = await register(api_client)

    created = await create_workflow(
        api_client,
        account.headers(),
        shipment_exception_definition(),
        name=SHIPMENT_EXCEPTION_WORKFLOW,
    )
    activated = await activate(api_client, account.headers(), created["id"])

    assert activated["status"] == "active"
    assert SHIPMENT_EXCEPTION_DESCRIPTION  # the description the demo ships with


async def test_only_an_administrator_may_activate(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)
    created = await create_workflow(api_client, owner.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/activate", headers=member.headers()
    )

    assert response.status_code == 403


async def test_deactivating_stops_a_version_being_startable(api_client: AsyncClient) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await api_client.post(f"{WORKFLOWS_URL}/{workflow_id}/deactivate", headers=account.headers())
    response = await api_client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs", json={"input": {}}, headers=account.headers()
    )

    assert response.status_code == 409


# -- Versioning ---------------------------------------------------------------


async def test_a_second_draft_of_a_name_is_the_next_version(api_client: AsyncClient) -> None:
    account = await register(api_client)
    name = "Shipment review"

    first = await create_workflow(api_client, account.headers(), lookup_workflow(), name=name)
    second = await create_workflow(api_client, account.headers(), lookup_workflow(), name=name)

    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]


async def test_activating_a_version_retires_the_previous_one(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Activating says which edition is current, so the old one stops being
    startable in the same breath."""
    account = await register(api_client)
    name = "Shipment review"

    first_id = await published(api_client, account.headers(), lookup_workflow(), name=name)
    second = await create_workflow(api_client, account.headers(), lookup_workflow(), name=name)
    await activate(api_client, account.headers(), second["id"])

    first = await session.get(Workflow, uuid.UUID(first_id))
    assert first is not None
    await session.refresh(first)
    assert first.status is WorkflowStatus.INACTIVE

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{first_id}/runs", json={"input": {}}, headers=account.headers()
    )
    assert response.status_code == 409


async def test_two_organizations_may_use_the_same_workflow_name(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)

    await create_workflow(api_client, ours.headers(), lookup_workflow(), name="Review")
    await create_workflow(api_client, theirs.headers(), lookup_workflow(), name="Review")

    rows = (
        (await session.execute(select(Workflow).where(Workflow.name == "Review"))).scalars().all()
    )

    assert {row.organization_id for row in rows} == {ours.organization_id, theirs.organization_id}


# -- Reading ------------------------------------------------------------------


async def test_the_list_shows_only_this_organizations_workflows(
    api_client: AsyncClient,
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)

    await create_workflow(api_client, ours.headers(), lookup_workflow(), name="Ours")
    await create_workflow(api_client, theirs.headers(), lookup_workflow(), name="Theirs")

    listed = await api_client.get(WORKFLOWS_URL, headers=ours.headers())

    assert [item["name"] for item in listed.json()] == ["Ours"]


async def test_another_organization_cannot_read_a_workflow(api_client: AsyncClient) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    created = await create_workflow(api_client, ours.headers(), lookup_workflow())

    response = await api_client.get(f"{WORKFLOWS_URL}/{created['id']}", headers=theirs.headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workflow_not_found"


async def test_another_organization_cannot_activate_a_workflow(api_client: AsyncClient) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    created = await create_workflow(api_client, ours.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/activate", headers=theirs.headers()
    )

    assert response.status_code == 404, "not found, rather than forbidden"


async def test_a_workflow_cannot_be_activated_against_another_tenants_agent(
    api_client: AsyncClient,
) -> None:
    """Agent visibility is checked against the caller's verified organization."""
    account = await register(api_client)
    created = await create_workflow(
        api_client,
        account.headers(),
        {
            "entry": "a",
            "steps": [
                {
                    "id": "a",
                    "type": "agent_step",
                    "agent_id": str(uuid.uuid4()),
                    "message": "Hello.",
                    "next": None,
                }
            ],
        },
    )

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{created['id']}/activate", headers=account.headers()
    )

    assert response.status_code == 422
    assert any(
        "cannot use" in problem for problem in response.json()["error"]["details"]["problems"]
    )


async def test_reading_a_workflow_needs_authentication(api_client: AsyncClient) -> None:
    response = await api_client.get(WORKFLOWS_URL)

    assert response.status_code == 401
