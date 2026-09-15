"""The audit trail for durable runs and approvals.

Two things are being checked, and the second matters more than the first.

**That the events exist.** A run that was created, finished, failed, was
cancelled, paused for a person, or was approved or rejected by one all leave a
row, so a durable execution can be reconstructed from the trail alone.

**That the events say nothing they should not.** An audit trail is read by more
people than any other table in the system, and rather more often than anybody
expects when it is designed. Nothing a customer typed, nothing a tool returned,
no credential and no exception text may be in one.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.enums import MemberRole
from tests.integration.agent_helpers import (
    ANSWER,
    RUN_URL,
    answering,
    asking,
    body,
    script,
    seed_shipment,
)
from tests.integration.auth_helpers import add_member, register

pytestmark = pytest.mark.integration

CANCEL = "cancel_shipment"
SECRET_REFERENCE = "SHIP-SENSITIVE-001"
REASON = "The customer asked us to stop it."


async def events_of(session: AsyncSession, organization_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.organization_id == organization_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        .scalars()
        .all()
    )


def types_of(events: list[AuditEvent]) -> list[str]:
    return [event.event_type for event in events]


# -- Runs ---------------------------------------------------------------------


async def test_creation_and_completion_are_both_recorded(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    account = await register(api_client)

    run = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()

    events = await events_of(session, account.organization_id)

    assert types_of(events) == ["agent.run.created", "agent.run"]
    assert [event.action for event in events] == ["pending", "completed"]
    assert all(str(event.resource_id) == run["run_id"] for event in events)
    assert all(event.resource_type == "agent_run" for event in events)
    assert all(event.user_id == account.user_id for event in events)


async def test_a_failure_is_recorded_with_its_code(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking("get_shipment", shipment_reference="ABC123"))
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    events = await events_of(session, account.organization_id)
    finished = next(event for event in events if event.event_type == "agent.run")

    assert finished.action == "failed"
    assert finished.event_metadata["error_code"] == "agent_max_steps_exceeded"


async def test_a_paused_run_records_the_pause_and_the_request(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking(CANCEL, shipment_reference="ABC123", reason=REASON), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    run = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()

    events = await events_of(session, account.organization_id)

    assert types_of(events) == ["agent.run.created", "agent.run", "approval.requested"]

    paused = events[1]
    assert paused.action == "awaiting_approval"
    assert paused.event_metadata["tool_calls"] == [CANCEL]

    requested = events[2]
    assert requested.resource_type == "approval"
    assert str(requested.resource_id) == run["approval"]["id"]
    assert requested.event_metadata["tool_name"] == CANCEL
    assert requested.event_metadata["run_id"] == run["run_id"]


async def test_an_idempotent_replay_is_recorded_as_one(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """A retry is visible as a retry, and did not start a second run."""
    script(app, answering())
    account = await register(api_client)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    await api_client.post(RUN_URL, json=body(), headers=headers)
    await api_client.post(RUN_URL, json=body(), headers=headers)

    created = [
        event
        for event in await events_of(session, account.organization_id)
        if event.event_type == "agent.run.created"
    ]

    assert [event.event_metadata["replayed"] for event in created] == [False, True]
    assert all(event.event_metadata["idempotent"] is True for event in created)


async def test_the_idempotency_key_itself_is_never_recorded(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """It is a client-chosen token that may encode anything they like."""
    script(app, answering())
    account = await register(api_client)

    await api_client.post(
        RUN_URL,
        json=body(),
        headers={**account.headers(), "Idempotency-Key": "customer-4471-invoice-99"},
    )

    events = await events_of(session, account.organization_id)
    recorded = str([event.event_metadata for event in events])
    assert "customer-4471" not in recorded


# -- Approvals ----------------------------------------------------------------


async def test_an_approval_is_recorded_with_who_decided(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking(CANCEL, shipment_reference="ABC123", reason=REASON), answering())
    owner = await register(api_client)
    admin = await add_member(api_client, session, owner.organization_id, MemberRole.ADMIN)
    await seed_shipment(session, owner.organization_id)

    run = (await api_client.post(RUN_URL, json=body(), headers=owner.headers())).json()
    await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/approve", headers=admin.headers()
    )

    events = await events_of(session, owner.organization_id)
    decision = next(event for event in events if event.event_type == "approval.approved")

    assert decision.action == "approved"
    assert decision.user_id == admin.user_id, "who decided"
    assert decision.event_metadata["requested_by"] == str(owner.user_id), "who asked"
    assert decision.event_metadata["tool_name"] == CANCEL
    assert decision.event_metadata["run_id"] == run["run_id"]


async def test_a_rejection_is_recorded_as_a_rejection(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking(CANCEL, shipment_reference="ABC123", reason=REASON), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    run = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()
    await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/reject", headers=account.headers()
    )

    types = types_of(await events_of(session, account.organization_id))

    assert "approval.rejected" in types
    assert "approval.approved" not in types


async def test_the_whole_story_of_an_approved_run_is_in_the_trail(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """One run, one conversation, a gap where a person was thinking - and a
    trail that reads in order."""
    script(app, asking(CANCEL, shipment_reference="ABC123", reason=REASON), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    run = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()
    await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/approve", headers=account.headers()
    )

    events = await events_of(session, account.organization_id)

    assert types_of(events) == [
        "agent.run.created",
        "agent.run",
        "approval.requested",
        "approval.approved",
        "agent.run",
    ]
    assert [event.action for event in events] == [
        "pending",
        "awaiting_approval",
        "pending",
        "approved",
        "completed",
    ]

    run_events = [event for event in events if event.resource_type == "agent_run"]
    assert {str(event.resource_id) for event in run_events} == {run["run_id"]}, "one run throughout"


# -- What must never be in one ------------------------------------------------


async def test_no_audit_row_holds_the_conversation_or_the_arguments(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking(CANCEL, shipment_reference=SECRET_REFERENCE, reason=REASON), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id, reference=SECRET_REFERENCE)

    run = (
        await api_client.post(
            RUN_URL,
            json=body(f"Cancel {SECRET_REFERENCE} - {REASON}"),
            headers=account.headers(),
        )
    ).json()
    await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/approve", headers=account.headers()
    )

    recorded = str(
        [
            (event.event_type, event.action, event.event_metadata)
            for event in await events_of(session, account.organization_id)
        ]
    )

    assert SECRET_REFERENCE not in recorded, "not the question, and not the arguments"
    assert REASON not in recorded, "not the reason the user gave"
    assert ANSWER not in recorded, "not the answer"
    assert "Rotterdam" not in recorded, "not what the tool returned"
    assert "Bearer" not in recorded and "password" not in recorded


async def test_the_trail_is_scoped_to_one_organization(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    ours = await register(api_client)
    theirs = await register(api_client)

    await api_client.post(RUN_URL, json=body(), headers=ours.headers())
    await api_client.post(RUN_URL, json=body(), headers=theirs.headers())

    assert len(await events_of(session, ours.organization_id)) == 2
    assert len(await events_of(session, theirs.organization_id)) == 2
