"""Approvals that ran out of time, and runs somebody stopped.

Three ways out of ``pending`` that are not a decision, and the point of the file
is that they are all the same shape underneath - a conditional ``UPDATE`` with
``pending`` in the ``WHERE`` clause - so the interesting cases are the ones where
two of them arrive together.

    expired    the clock; nobody answered
    cancelled  a person stopped the whole run the action belonged to
    decided    a person answered

Whichever gets the row lock first wins, and the others are told so. What must
never happen is the shipment being cancelled by a losing branch, so the
assertion that carries the most weight in this file is a status check on a
business record, repeated after every race.

The races are run on genuinely concurrent connections. Two sequential calls
through one session prove nothing about a lock neither of them contended for.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import AgentRunStatus
from app.core.config import Settings
from app.core.database import engine, get_session
from app.main import create_app
from app.models.agent_run import AgentRunRecord, ToolExecutionRecord
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.business import Shipment
from app.models.enums import ApprovalStatus, MemberRole, RunStatus, ShipmentStatus
from app.models.workflow import WorkflowRun
from app.services.approval_expiry import EXPIRED_ERROR_CODE, expire_due
from tests.integration.agent_helpers import RUN_URL, answering, body, script, seed_shipment
from tests.integration.auth_helpers import add_member, register
from tests.integration.test_approval_workflow import (
    CANCEL,
    cancelling,
    pause_a_run,
    shipment_status,
)
from tests.integration.test_workflow_approval import cancelling_workflow, payload
from tests.integration.workflow_helpers import published, seed, start

pytestmark = pytest.mark.integration

APPROVALS_URL = "/api/v1/approvals"


def decide_url(approval_id: str, *, approve: bool) -> str:
    return f"{APPROVALS_URL}/{approval_id}/{'approve' if approve else 'reject'}"


async def backdate(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """Move every pending deadline into the past.

    The deadline is a day away by configuration, and a test that waited for it
    would take a day. Moving the row is the honest shortcut: it exercises the
    same column, the same comparison and the same statement that a real lapse
    would, and changes nothing about how the lapse is decided.
    """
    await session.execute(
        update(Approval)
        .where(
            Approval.organization_id == organization_id,
            Approval.status == ApprovalStatus.PENDING,
        )
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def the_approval(session: AsyncSession, organization_id: uuid.UUID) -> Approval:
    approval = (
        (await session.execute(select(Approval).where(Approval.organization_id == organization_id)))
        .scalars()
        .one()
    )
    await session.refresh(approval)
    return approval


# -- A deadline exists at all -------------------------------------------------


async def test_an_approval_is_given_a_deadline_when_it_is_requested(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    approval = await the_approval(session, account.organization_id)

    assert approval.expires_at is not None
    assert approval.expires_at > datetime.now(UTC)


async def test_the_deadline_comes_from_configuration(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Stamped from the setting in force when the approval was requested, so
    changing the setting later cannot move a deadline somebody was given."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    approval = await the_approval(session, account.organization_id)
    configured = Settings(app_env="test").approval_expiration_seconds

    assert approval.expires_at is not None
    remaining = (approval.expires_at - approval.requested_at).total_seconds()
    assert abs(remaining - configured) < 30


# -- Lapsing ------------------------------------------------------------------


async def test_an_overdue_approval_lapses_when_the_queue_is_read(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """There is no background worker; somebody looking is what does it."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    page = await api_client.get(APPROVALS_URL, headers=account.headers())

    assert page.json()["approvals"] == []
    assert (await the_approval(session, account.organization_id)).status is ApprovalStatus.EXPIRED


async def test_an_expired_approval_cannot_be_approved(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "approval_expired"


async def test_an_already_swept_approval_still_answers_expired(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The case the acceptance walkthrough found.

    Once a sweep has moved the row to ``expired``, the conditional update fails
    on the *state* rather than on the deadline - and reporting that as "already
    decided" would be false twice over: nobody decided it, and the client would
    be told something a retry might fix. Whoever is looking at a stale screen
    gets 410 either way.
    """
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    # Somebody reads the queue first, which is what sweeps.
    await api_client.get(APPROVALS_URL, headers=account.headers())
    assert (await the_approval(session, account.organization_id)).status is ApprovalStatus.EXPIRED

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "approval_expired"


async def test_a_cancelled_approval_is_a_conflict_not_an_expiry(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Withdrawn is not lapsed, and the codes keep them apart."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=account.headers())

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_already_decided"


async def test_an_expired_approval_never_runs_the_action(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The number that matters, again: zero."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert await shipment_status(session, account.organization_id) is not ShipmentStatus.CANCELLED

    executions = (
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [execution.executed for execution in executions] == [False]


async def test_a_lapse_is_not_a_rejection(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Nobody refused this, and the record must not say somebody did."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    await expire_due(session, organization_id=account.organization_id)

    approval = await the_approval(session, account.organization_id)
    assert approval.status is ApprovalStatus.EXPIRED
    assert approval.approved_by is None, "nobody decided it"
    assert approval.decision_reason is None


async def test_the_paused_agent_run_is_stopped_rather_than_left_waiting(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    await expire_due(session, organization_id=account.organization_id)

    record = await session.get(AgentRunRecord, uuid.UUID(paused["run_id"]))
    assert record is not None
    await session.refresh(record)
    assert record.status is AgentRunStatus.FAILED
    assert record.error_code == EXPIRED_ERROR_CODE


async def test_the_paused_workflow_run_is_stopped_too(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())
    assert paused["status"] == "awaiting_approval"

    await backdate(session, account.organization_id)
    await expire_due(session, organization_id=account.organization_id)

    run = await session.get(WorkflowRun, uuid.UUID(paused["run_id"]))
    assert run is not None
    await session.refresh(run)
    assert run.status is RunStatus.FAILED
    assert run.error_code == EXPIRED_ERROR_CODE
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.EXCEPTION


async def test_a_lapse_is_recorded_in_the_trail(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    await expire_due(session, organization_id=account.organization_id)

    events = (
        (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.organization_id == account.organization_id,
                    AuditEvent.event_type == "approval.expired",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].action == "expired"
    assert events[0].event_metadata["tool_name"] == CANCEL


async def test_a_second_sweep_lapses_nothing_twice(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)
    await backdate(session, account.organization_id)

    first = await expire_due(session, organization_id=account.organization_id)
    second = await expire_due(session, organization_id=account.organization_id)

    assert (first, second) == (1, 0)


async def test_a_sweep_leaves_another_organization_alone(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    await pause_a_run(app, api_client, session, theirs)
    await backdate(session, theirs.organization_id)

    swept = await expire_due(session, organization_id=ours.organization_id)

    assert swept == 0
    assert (await the_approval(session, theirs.organization_id)).status is ApprovalStatus.PENDING


# -- Cancelling a paused run --------------------------------------------------


async def test_an_administrator_can_stop_a_paused_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.post(
        f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=account.headers()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_cancelling_withdraws_the_question(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Cancelled, not rejected: nobody refused the action."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    await api_client.post(f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=account.headers())

    approval = await the_approval(session, account.organization_id)
    assert approval.status is ApprovalStatus.CANCELLED
    assert approval.approved_by is None


async def test_nothing_runs_after_a_run_is_cancelled(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    await api_client.post(f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=account.headers())
    decided = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert decided.status_code == 409
    assert await shipment_status(session, account.organization_id) is not ShipmentStatus.CANCELLED


async def test_a_member_cannot_stop_a_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)
    paused = await pause_a_run(app, api_client, session, owner)

    response = await api_client.post(
        f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=member.headers()
    )

    assert response.status_code == 403


async def test_a_finished_run_cannot_be_cancelled(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Only a *waiting* run can be stopped. A finished one has nothing to stop,
    and a running one is being driven by a request this would race."""
    account = await register(api_client)
    script(app, answering("Nothing to do."))

    run = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()
    response = await api_client.post(
        f"/api/v1/ai/runs/{run['run_id']}/cancel", headers=account.headers()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "agent_run_not_cancellable"


async def test_another_organization_cannot_stop_the_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    paused = await pause_a_run(app, api_client, session, ours)

    response = await api_client.post(
        f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=theirs.headers()
    )

    assert response.status_code == 404


async def test_a_paused_workflow_run_can_be_stopped(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    response = await api_client.post(
        f"/api/v1/ai/workflows/{workflow_id}/runs/{paused['run_id']}/cancel",
        headers=account.headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.EXCEPTION
    assert (await the_approval(session, account.organization_id)).status is ApprovalStatus.CANCELLED


async def test_a_cancelled_workflow_step_does_not_still_say_it_is_waiting(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    cancelled = await api_client.post(
        f"/api/v1/ai/workflows/{workflow_id}/runs/{paused['run_id']}/cancel",
        headers=account.headers(),
    )

    assert [step["status"] for step in cancelled.json()["steps"]] == ["cancelled"]


# -- Two things at once -------------------------------------------------------


def committing_client(factory: async_sessionmaker[AsyncSession], *decisions: object) -> AsyncClient:
    """A client whose requests each get their own session, and commit.

    Deliberately not the shared-session fixture: two requests inside one
    transaction cannot contend for a row lock, which would make every race in
    this section unobservable.
    """
    settings = Settings(
        app_env="test", argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1
    )
    application = create_app(settings)
    if decisions:
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


async def remove(factory: async_sessionmaker[AsyncSession], account: object) -> None:
    async with factory() as cleanup:
        await cleanup.execute(
            text("DELETE FROM organizations WHERE id = :id"),
            {"id": account.organization_id},  # type: ignore[attr-defined]
        )
        await cleanup.execute(
            text("DELETE FROM users WHERE id = :id"),
            {"id": account.user_id},  # type: ignore[attr-defined]
        )
        await cleanup.commit()


async def test_approve_and_reject_together_leave_one_decision(database: None) -> None:
    """Two administrators disagreeing at the same instant.

    One decision is recorded, the other is told it is too late, and the shipment
    reflects whichever won - never both, and never neither.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    setup_client = committing_client(factory, cancelling(), answering())
    approver = committing_client(factory, answering())
    rejecter = committing_client(factory, answering())

    account = None
    try:
        async with setup_client, approver, rejecter:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed_shipment(seeding, account.organization_id)
                await seeding.commit()

            paused = (
                await setup_client.post(RUN_URL, json=body(), headers=account.headers())
            ).json()
            assert paused["status"] == "awaiting_approval", paused

            approval_id = paused["approval"]["id"]
            approved, rejected = await asyncio.gather(
                approver.post(decide_url(approval_id, approve=True), headers=account.headers()),
                rejecter.post(decide_url(approval_id, approve=False), headers=account.headers()),
            )

        assert sorted([approved.status_code, rejected.status_code]) == [200, 409]

        async with factory() as checking:
            approval = (
                (
                    await checking.execute(
                        select(Approval).where(Approval.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
            assert approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}

            shipment = (
                (
                    await checking.execute(
                        select(Shipment).where(Shipment.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
            expected = (
                ShipmentStatus.CANCELLED
                if approval.status is ApprovalStatus.APPROVED
                else ShipmentStatus.EXCEPTION
            )
            assert shipment.status is expected, "the shipment agrees with the decision that won"
    finally:
        if account is not None:
            await remove(factory, account)


async def test_approve_and_expire_together_leave_one_outcome(database: None) -> None:
    """The clock and an administrator, arriving together.

    The deadline is in the same ``WHERE`` clause as the state, so this is one
    race rather than a check followed by a write. Either the decision lands
    before the deadline or the lapse does; the shipment is cancelled only in the
    first case.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    setup_client = committing_client(factory, cancelling(), answering())
    approver = committing_client(factory, answering())
    sweeper = committing_client(factory)

    account = None
    try:
        async with setup_client, approver, sweeper:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed_shipment(seeding, account.organization_id)
                await seeding.commit()

            paused = (
                await setup_client.post(RUN_URL, json=body(), headers=account.headers())
            ).json()
            assert paused["status"] == "awaiting_approval", paused

            # Overdue as of now. Both racers are looking at the same row.
            async with factory() as ageing:
                await backdate(ageing, account.organization_id)

            decided, listed = await asyncio.gather(
                approver.post(
                    decide_url(paused["approval"]["id"], approve=True),
                    headers=account.headers(),
                ),
                # Reading the queue is what sweeps, so this *is* the expiry path.
                sweeper.get(APPROVALS_URL, headers=account.headers()),
            )

        assert listed.status_code == 200
        assert decided.status_code == 410, "the deadline had already passed"

        async with factory() as checking:
            approval = (
                (
                    await checking.execute(
                        select(Approval).where(Approval.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
            assert approval.status is ApprovalStatus.EXPIRED

            shipment = (
                (
                    await checking.execute(
                        select(Shipment).where(Shipment.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
            assert shipment.status is not ShipmentStatus.CANCELLED, "an expiry cancels nothing"
    finally:
        if account is not None:
            await remove(factory, account)


async def test_approve_and_cancel_together_leave_one_outcome(database: None) -> None:
    """A person deciding and a person stopping the whole run.

    Exactly one succeeds. If the cancellation wins, the decision finds the run
    no longer paused and reports that it cannot continue - having executed
    nothing, which is the property the shipment check below is for.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    setup_client = committing_client(factory, cancelling(), answering())
    approver = committing_client(factory, answering())
    canceller = committing_client(factory)

    account = None
    try:
        async with setup_client, approver, canceller:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed_shipment(seeding, account.organization_id)
                await seeding.commit()

            paused = (
                await setup_client.post(RUN_URL, json=body(), headers=account.headers())
            ).json()
            assert paused["status"] == "awaiting_approval", paused

            decided, cancelled = await asyncio.gather(
                approver.post(
                    decide_url(paused["approval"]["id"], approve=True),
                    headers=account.headers(),
                ),
                canceller.post(
                    f"/api/v1/ai/runs/{paused['run_id']}/cancel", headers=account.headers()
                ),
            )

        outcomes = {decided.status_code, cancelled.status_code}
        assert 409 in outcomes, f"one of them must lose: {decided.text} / {cancelled.text}"

        async with factory() as checking:
            shipment = (
                (
                    await checking.execute(
                        select(Shipment).where(Shipment.organization_id == account.organization_id)
                    )
                )
                .scalars()
                .one()
            )
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

            if decided.status_code == 200:
                assert shipment.status is ShipmentStatus.CANCELLED
                assert [e.executed for e in executions] == [True]
            else:
                assert shipment.status is not ShipmentStatus.CANCELLED
                assert [e.executed for e in executions] == [False]
    finally:
        if account is not None:
            await remove(factory, account)
