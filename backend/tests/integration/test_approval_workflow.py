"""The human approval workflow, end to end.

A destructive tool, a run that pauses, a person who decides, and the same run
carrying on afterwards. The assertions that matter most are the negative ones -
how many times the shipment was actually cancelled - because every other
property here is in service of that number being zero or one and never two.

The gateway is scripted; everything below it is real, including the tool that
writes to the ``shipments`` table.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import AgentRunStatus
from app.core.config import Settings
from app.core.database import engine, get_session
from app.main import create_app
from app.models.agent_run import AgentRunRecord, ToolExecutionRecord
from app.models.approval import Approval
from app.models.business import Shipment
from app.models.enums import ApprovalStatus, MemberRole, ShipmentStatus
from tests.integration.agent_helpers import (
    REFUSED_ANSWER,
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
REASON = "The customer asked us to stop it."


def cancelling(reference: str = "ABC123") -> object:
    return asking(CANCEL, shipment_reference=reference, reason=REASON)


async def pause_a_run(
    app: FastAPI,
    api_client: AsyncClient,
    session: AsyncSession,
    account,  # noqa: ANN001 - tests.integration.auth_helpers.Account
    *,
    after: str = REFUSED_ANSWER,
) -> dict:
    """Run the agent until it asks to cancel a shipment, and stop there.

    The script's second decision is what the agent says once it has been told
    what happened - reached only when the run resumes.
    """
    script(app, cancelling(), answering(after))
    await seed_shipment(session, account.organization_id)

    response = await api_client.post(RUN_URL, json=body(), headers=account.headers())
    assert response.status_code == 200, response.text
    return response.json()


async def shipment_status(session: AsyncSession, organization_id: uuid.UUID) -> ShipmentStatus:
    shipment = (
        (await session.execute(select(Shipment).where(Shipment.organization_id == organization_id)))
        .scalars()
        .one()
    )
    await session.refresh(shipment)
    return shipment.status


async def executions_of(
    session: AsyncSession, organization_id: uuid.UUID
) -> list[ToolExecutionRecord]:
    return list(
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.organization_id == organization_id
                )
            )
        )
        .scalars()
        .all()
    )


# -- Asking -------------------------------------------------------------------


async def test_a_destructive_tool_pauses_the_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)

    run = await pause_a_run(app, api_client, session, account)

    assert run["status"] == "awaiting_approval"
    assert run["final_response"] is None, "a paused run has not answered"
    assert run["approval"]["tool_name"] == CANCEL
    assert run["approval"]["reason"]


async def test_nothing_is_cancelled_while_a_decision_is_pending(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The number that matters: zero."""
    account = await register(api_client)

    await pause_a_run(app, api_client, session, account)

    assert await shipment_status(session, account.organization_id) is ShipmentStatus.IN_TRANSIT

    executions = await executions_of(session, account.organization_id)
    assert len(executions) == 1
    assert executions[0].executed is False
    assert executions[0].outcome == "approval_required"
    assert executions[0].requires_approval is True
    assert executions[0].safety == "destructive"


async def test_the_approval_row_carries_the_execution_it_authorises(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)

    run = await pause_a_run(app, api_client, session, account)

    approval = (
        (
            await session.execute(
                select(Approval).where(Approval.organization_id == account.organization_id)
            )
        )
        .scalars()
        .one()
    )

    assert approval.status is ApprovalStatus.PENDING
    assert str(approval.run_id) == run["run_id"]
    assert approval.tool_name == CANCEL
    assert approval.requested_by == account.user_id
    assert approval.tool_execution_id is not None


async def test_an_agent_approval_leaves_the_parameters_column_empty(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The column exists for workflow approvals and is not the agent's to use.

    Tool arguments are business data. They live in the conversation, beside the
    turn that proposed them; an approval authorises an *execution* by its id, so
    copying the values into a second, operational place would disclose them to
    every reader of the queue for no benefit at all.
    """
    account = await register(api_client)

    await pause_a_run(app, api_client, session, account)

    approval = (
        (
            await session.execute(
                select(Approval).where(Approval.organization_id == account.organization_id)
            )
        )
        .scalars()
        .one()
    )

    assert approval.parameters == {}
    assert REASON not in str(approval.parameters)
    assert "ABC123" not in str(approval.parameters)


async def test_the_queue_publishes_the_declared_summary_and_no_payload(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """What changed in the human-in-the-loop phase, and what did not.

    This test used to assert that the reference never appeared in the queue at
    all, which was true and was also why the queue was close to useless: an
    approver saw "cancel_shipment" and had to guess which consignment. It now
    appears, and the reason it is safe to let it is a different mechanism rather
    than a relaxed rule - ``CancelShipmentTool`` declares, in its own code,
    which of its fields a reviewer may be shown, and the projection was taken
    once when the approval was requested.

    So the assertions here are about that declaration: the summary says what is
    being done and to what, the fields are exactly the declared ones, and the
    argument payload has no representation in the response at all.
    """
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    listed = await api_client.get("/api/v1/approvals", headers=account.headers())

    assert listed.status_code == 200
    queued = listed.json()["approvals"]
    assert len(queued) == 1

    approval = queued[0]
    assert approval["tool_name"] == CANCEL, "an approver is told what kind of action it is"
    assert approval["summary"] == "Cancel shipment ABC123", "and which record it lands on"
    assert [field["label"] for field in approval["summary_fields"]] == [
        "Shipment reference",
        "Reason",
    ]

    # The payload itself has nowhere to be: no field carries it, and the column
    # that could have is still empty.
    assert "parameters" not in approval
    assert "arguments" not in approval


# -- Who may decide -----------------------------------------------------------


async def test_a_member_may_read_the_queue(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)

    await pause_a_run(app, api_client, session, owner)

    listed = await api_client.get("/api/v1/approvals", headers=member.headers())

    assert listed.status_code == 200
    assert len(listed.json()["approvals"]) == 1


async def test_a_member_cannot_approve(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The backend is the boundary. A hidden button is not.

    This request carries a valid token and the right organization; only the role
    stops it, and it is checked against the database on every request.
    """
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)

    run = await pause_a_run(app, api_client, session, owner)
    approval_id = run["approval"]["id"]

    response = await api_client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers=member.headers()
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert await shipment_status(session, owner.organization_id) is ShipmentStatus.IN_TRANSIT


async def test_a_member_cannot_reject_either(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)

    run = await pause_a_run(app, api_client, session, owner)

    response = await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/reject", headers=member.headers()
    )

    assert response.status_code == 403


async def test_an_admin_may_approve(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    admin = await add_member(api_client, session, owner.organization_id, MemberRole.ADMIN)

    run = await pause_a_run(app, api_client, session, owner)

    response = await api_client.post(
        f"/api/v1/approvals/{run['approval']['id']}/approve", headers=admin.headers()
    )

    assert response.status_code == 200, response.text


async def test_an_unauthenticated_caller_cannot_approve(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    run = await pause_a_run(app, api_client, session, owner)

    response = await api_client.post(f"/api/v1/approvals/{run['approval']['id']}/approve")

    assert response.status_code == 401
    assert await shipment_status(session, owner.organization_id) is ShipmentStatus.IN_TRANSIT


# -- Approving ----------------------------------------------------------------


async def test_approval_executes_the_tool_exactly_once_and_resumes_the_same_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The whole workflow, in one test, because the parts only mean anything
    together."""
    account = await register(api_client)
    paused = await pause_a_run(
        app, api_client, session, account, after="ABC123 has been cancelled."
    )

    resumed = await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
    )

    assert resumed.status_code == 200, resumed.text
    run = resumed.json()["agent_run"]

    assert run is not None, "the decision says which process it resumed"
    assert run["run_id"] == paused["run_id"], "the same run, not a successor"
    assert run["status"] == "completed"
    assert run["final_response"] == "ABC123 has been cancelled."
    assert run["approval"] is None, "nothing is waiting any more"

    assert await shipment_status(session, account.organization_id) is ShipmentStatus.CANCELLED

    executions = await executions_of(session, account.organization_id)
    assert len(executions) == 1, "the same execution, updated - not a second one"
    assert executions[0].executed is True
    assert executions[0].outcome == "succeeded"


async def test_the_resumed_run_keeps_its_step_budget(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Resuming is not a fresh allowance. The budget bounds the run."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    resumed = (
        await api_client.post(
            f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
        )
    ).json()["agent_run"]

    assert paused["step_count"] == 1
    assert resumed["step_count"] == 2, "the second step, not the first of a new run"


async def test_the_execution_identity_survives_the_pause(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """One tool_execution_id from request to approval to result."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    approval = (
        await api_client.get(
            f"/api/v1/approvals/{paused['approval']['id']}", headers=account.headers()
        )
    ).json()

    await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
    )

    executions = await executions_of(session, account.organization_id)
    assert str(executions[0].id) == approval["tool_execution_id"]


async def test_approving_twice_does_not_cancel_twice(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """A second approval is a conflict, not a second cancellation."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    url = f"/api/v1/approvals/{paused['approval']['id']}/approve"

    first = await api_client.post(url, headers=account.headers())
    second = await api_client.post(url, headers=account.headers())

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "approval_already_decided"

    executions = await executions_of(session, account.organization_id)
    assert len([e for e in executions if e.executed]) == 1


async def test_a_duplicate_approval_request_still_executes_once(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Retrying the paused run with its key, then approving, cancels once."""
    account = await register(api_client)
    script(app, cancelling(), answering())
    await seed_shipment(session, account.organization_id)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    first = (await api_client.post(RUN_URL, json=body(), headers=headers)).json()
    replayed = (await api_client.post(RUN_URL, json=body(), headers=headers)).json()

    assert replayed["approval"]["id"] == first["approval"]["id"], "one approval, not two"

    await api_client.post(
        f"/api/v1/approvals/{first['approval']['id']}/approve", headers=account.headers()
    )

    executions = await executions_of(session, account.organization_id)
    assert len(executions) == 1
    assert executions[0].executed is True


# -- Rejecting ----------------------------------------------------------------


async def test_rejection_never_executes_the_tool(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    rejected = await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/reject", headers=account.headers()
    )

    assert rejected.status_code == 200, rejected.text
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.IN_TRANSIT

    executions = await executions_of(session, account.organization_id)
    assert executions[0].executed is False
    assert executions[0].outcome == "rejected"


async def test_a_rejection_is_a_decision_not_a_failure(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The run completes, having been told no, and says so in its own words.

    Turning a human refusal into an error would be wrong twice over: it would
    read as something broken, and it would deny the user the one thing they
    actually need to be told.
    """
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account, after=REFUSED_ANSWER)

    resumed = (
        await api_client.post(
            f"/api/v1/approvals/{paused['approval']['id']}/reject", headers=account.headers()
        )
    ).json()["agent_run"]

    assert resumed["run_id"] == paused["run_id"]
    assert resumed["status"] == "completed"
    assert resumed["error_code"] is None, "nothing failed"
    assert resumed["final_response"] == REFUSED_ANSWER
    assert resumed["tool_calls"][0]["outcome"] == "rejected"


async def test_the_model_is_told_it_was_refused(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The refusal reaches the agent as a tool result, like any other outcome."""
    account = await register(api_client)
    provider = script(app, cancelling(), answering(REFUSED_ANSWER))
    await seed_shipment(session, account.organization_id)

    paused = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()
    await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/reject", headers=account.headers()
    )

    assert len(provider.requests) == 2, "the model was asked again after the decision"
    last = provider.requests[-1]
    shown = "\n".join(message.transport_content for message in last.messages)

    assert "rejected" in shown
    assert "declined" in shown.lower()


async def test_approving_after_a_rejection_is_refused(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    approval_id = paused["approval"]["id"]

    await api_client.post(f"/api/v1/approvals/{approval_id}/reject", headers=account.headers())
    late = await api_client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers=account.headers()
    )

    assert late.status_code == 409
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.IN_TRANSIT


async def test_rejecting_after_an_approval_is_refused(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The action has already happened; a later "no" cannot unhappen it, and
    must not pretend to."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    approval_id = paused["approval"]["id"]

    await api_client.post(f"/api/v1/approvals/{approval_id}/approve", headers=account.headers())
    late = await api_client.post(
        f"/api/v1/approvals/{approval_id}/reject", headers=account.headers()
    )

    assert late.status_code == 409
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.CANCELLED


# -- Tenant isolation ---------------------------------------------------------


async def test_another_organization_cannot_see_the_approval(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)

    paused = await pause_a_run(app, api_client, session, ours)

    listed = await api_client.get("/api/v1/approvals", headers=theirs.headers())
    fetched = await api_client.get(
        f"/api/v1/approvals/{paused['approval']['id']}", headers=theirs.headers()
    )

    assert listed.json()["approvals"] == []
    assert fetched.status_code == 404
    assert fetched.json()["error"]["code"] == "approval_not_found"


async def test_another_organizations_owner_cannot_approve(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Being an owner somewhere is not being an owner here."""
    ours = await register(api_client)
    theirs = await register(api_client)

    paused = await pause_a_run(app, api_client, session, ours)

    response = await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=theirs.headers()
    )

    assert response.status_code == 404, "not found, rather than forbidden"
    assert await shipment_status(session, ours.organization_id) is ShipmentStatus.IN_TRANSIT


async def test_an_approval_cannot_reference_another_tenants_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Refused by the database. The composite key carries the organization."""
    ours = await register(api_client)
    theirs = await register(api_client)

    record = AgentRunRecord(
        organization_id=theirs.organization_id,
        user_id=theirs.user_id,
        agent_id=uuid.uuid4(),
    )
    session.add(record)
    await session.flush()

    session.add(
        Approval(
            organization_id=ours.organization_id,
            requested_by=ours.user_id,
            run_id=record.id,
            action=CANCEL,
        )
    )

    with pytest.raises(Exception) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


# -- Two administrators at once -----------------------------------------------


async def test_two_concurrent_approvals_cancel_the_shipment_once(database: None) -> None:
    """Two administrators, two connections, one pending approval.

    Sequential double-approval is covered above; this is the case the conditional
    UPDATE and the execution claim exist for. Nothing shares a transaction, so
    the two requests genuinely contend, and the rows are committed and cleaned up
    afterwards.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    def build(*decisions: object) -> AsyncClient:
        """One application per client, each with its own scripted provider.

        The decisions are per client on purpose. A client that only ever serves
        the *resume* must answer rather than ask for the tool again - its
        provider has not seen the first call, so a shared script would replay it
        and the agent would request a second cancellation.
        """
        settings = Settings(
            app_env="test", argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1
        )
        application = create_app(settings)
        script(application, *decisions)

        async def own_session() -> AsyncIterator[AsyncSession]:
            async with factory() as own:
                try:
                    yield own
                except Exception:
                    await own.rollback()
                    raise
                else:
                    await own.commit()

        application.dependency_overrides[get_session] = own_session
        return AsyncClient(transport=ASGITransport(app=application), base_url="http://testserver")

    setup_client = build(cancelling(), answering())
    first_client = build(answering())
    second_client = build(answering())

    account = None
    try:
        async with setup_client, first_client, second_client:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed_shipment(seeding, account.organization_id)
                await seeding.commit()

            paused = (
                await setup_client.post(RUN_URL, json=body(), headers=account.headers())
            ).json()
            assert paused["status"] == "awaiting_approval", paused

            url = f"/api/v1/approvals/{paused['approval']['id']}/approve"
            first, second = await asyncio.gather(
                first_client.post(url, headers=account.headers()),
                second_client.post(url, headers=account.headers()),
            )

        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 409], f"{first.text} / {second.text}"

        async with factory() as checking:
            executions = (
                (
                    await checking.execute(
                        select(ToolExecutionRecord).where(
                            ToolExecutionRecord.organization_id == account.organization_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(executions) == 1
            assert executions[0].executed is True

            shipment = (
                (
                    await checking.execute(
                        select(Shipment).where(Shipment.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
            assert shipment.status is ShipmentStatus.CANCELLED
    finally:
        if account is not None:
            async with factory() as cleanup:
                await cleanup.execute(
                    text("DELETE FROM organizations WHERE id = :id"),
                    {"id": account.organization_id},
                )
                await cleanup.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": account.user_id}
                )
                await cleanup.commit()


# -- The run record -----------------------------------------------------------


async def test_the_paused_run_is_durable_and_readable_later(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Somebody who closed the page comes back and finds it where they left it."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    fetched = await api_client.get(f"/api/v1/ai/runs/{paused['run_id']}", headers=account.headers())

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "awaiting_approval"
    assert fetched.json()["approval"]["id"] == paused["approval"]["id"]

    record = await session.get(AgentRunRecord, uuid.UUID(paused["run_id"]))
    assert record is not None
    await session.refresh(record)
    assert record.status is AgentRunStatus.AWAITING_APPROVAL
