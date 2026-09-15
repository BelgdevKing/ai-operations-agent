"""Shared ground for the workflow integration tests.

Everything below the vendor boundary is real: the engine, the tool framework,
the business tools, the approval table and PostgreSQL. Only the provider is
scripted, and only for the tests that involve an agent step.

A workflow is created and activated through the API rather than inserted,
because activation is where validation happens and a fixture that skipped it
would be testing a workflow the platform would refuse to run.
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ShipmentStatus
from tests.integration.agent_helpers import seed_shipment

WORKFLOWS_URL = "/api/v1/ai/workflows"

REFERENCE = "WF-ABC123"
REASON = "The customer asked us to stop it."


def tool_step(
    step_id: str,
    tool: str,
    *,
    arguments: dict[str, Any] | None = None,
    next_step: str | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "type": "tool_call",
        "tool": tool,
        "arguments": arguments or {},
        "next": next_step,
    }


def condition_step(
    step_id: str,
    *,
    field: str,
    operator: str = "equals",
    value: Any = None,
    on_true: str | None = None,
    on_false: str | None = None,
) -> dict[str, Any]:
    condition: dict[str, Any] = {"field": field, "operator": operator}
    if operator not in {"exists", "not_exists"}:
        condition["value"] = value

    return {
        "id": step_id,
        "type": "condition",
        "condition": condition,
        "on_true": on_true,
        "on_false": on_false,
    }


def approval_step(
    step_id: str,
    *,
    reason: str = "Somebody should agree to this.",
    next_step: str | None = None,
    on_reject: str | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "type": "approval",
        "reason": reason,
        "next": next_step,
        "on_reject": on_reject,
    }


def lookup_workflow() -> dict[str, Any]:
    """Two real lookups, with data flowing from the input into both."""
    return {
        "entry": "look",
        "steps": [
            tool_step(
                "look",
                "get_shipment",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step="charges",
            ),
            tool_step(
                "charges",
                "get_shipment_charges",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step=None,
            ),
        ],
    }


async def create_workflow(
    client: AsyncClient,
    headers: dict[str, str],
    definition: dict[str, Any],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Draft a version. Not runnable until it is activated."""
    response = await client.post(
        WORKFLOWS_URL,
        json={
            "name": name or f"Workflow {uuid.uuid4().hex[:8]}",
            "description": "A test workflow.",
            "definition": definition,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def activate(
    client: AsyncClient, headers: dict[str, str], workflow_id: str
) -> dict[str, Any]:
    response = await client.post(f"{WORKFLOWS_URL}/{workflow_id}/activate", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def published(
    client: AsyncClient,
    headers: dict[str, str],
    definition: dict[str, Any],
    *,
    name: str | None = None,
) -> str:
    """A workflow that is ready to run, and its id."""
    created = await create_workflow(client, headers, definition, name=name)
    await activate(client, headers, created["id"])
    return str(created["id"])


async def start(
    client: AsyncClient,
    headers: dict[str, str],
    workflow_id: str,
    *,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if idempotency_key:
        headers = {**headers, "Idempotency-Key": idempotency_key}

    response = await client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs",
        json={"input": payload or {"shipment_reference": REFERENCE}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def seed(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    status: ShipmentStatus = ShipmentStatus.EXCEPTION,
    reference: str = REFERENCE,
) -> None:
    """One shipment for a workflow to act on, in exception by default.

    In exception because that is the branch the demonstration workflow is
    interesting on - a shipment travelling normally takes the other one, which
    the tests also cover.
    """
    await seed_shipment(session, organization_id, reference=reference, status=status)
