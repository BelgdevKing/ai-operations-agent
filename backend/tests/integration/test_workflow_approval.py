"""Workflows that stop and ask a person.

Two ways a workflow pauses, and both go through the Part 16 approval table:

* an **approval step**, gating a decision;
* a **tool step whose tool needs approval**, gating that exact execution.

The assertions that matter most are the counts - how many times the shipment was
actually cancelled - because every other property here exists so that number is
zero or one and never two.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import engine, get_session
from app.main import create_app
from app.models.agent_run import ToolExecutionRecord
from app.models.approval import Approval
from app.models.business import Shipment
from app.models.enums import ApprovalStatus, MemberRole, ShipmentStatus, StepRunStatus
from app.models.workflow import WorkflowRun, WorkflowStepRun
from tests.integration.auth_helpers import add_member, register
from tests.integration.workflow_helpers import (
    REASON,
    REFERENCE,
    WORKFLOWS_URL,
    approval_step,
    published,
    seed,
    start,
    tool_step,
)

pytestmark = pytest.mark.integration

CANCEL = "cancel_shipment"


def cancelling_workflow(*, confirm: bool = True) -> dict[str, object]:
    """Cancel a shipment, then read it back. The cancel step gates itself."""
    steps = [
        tool_step(
            "cancel",
            CANCEL,
            arguments={
                "shipment_reference": "$.input.shipment_reference",
                "reason": "$.input.reason",
            },
            next_step="confirm" if confirm else None,
        )
    ]
    if confirm:
        steps.append(
            tool_step(
                "confirm",
                "get_shipment",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step=None,
            )
        )
    return {"entry": "cancel", "steps": steps}


def gated_workflow(*, on_reject: str | None = None) -> dict[str, object]:
    """An explicit approval step in front of an ordinary lookup.

    The refusal path is only included when the definition declares one -
    otherwise the step would be unreachable, which activation refuses, and
    rightly: a step nothing can get to is a step somebody wrote by mistake.
    """
    steps: list[dict[str, object]] = [
        approval_step(
            "ask",
            reason="Somebody should agree before we look this up.",
            next_step="look",
            on_reject=on_reject,
        ),
        tool_step(
            "look",
            "get_shipment",
            arguments={"shipment_reference": "$.input.shipment_reference"},
            next_step=None,
        ),
    ]

    if on_reject is not None:
        steps.append(
            tool_step(
                on_reject,
                "get_shipment_charges",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step=None,
            )
        )

    return {"entry": "ask", "steps": steps}


def payload() -> dict[str, str]:
    return {"shipment_reference": REFERENCE, "reason": REASON}


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


def decide_url(approval_id: str, approve: bool) -> str:
    return f"/api/v1/approvals/{approval_id}/{'approve' if approve else 'reject'}"


# -- A gated tool step --------------------------------------------------------


async def test_a_destructive_tool_pauses_the_workflow(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())

    run = await start(api_client, account.headers(), workflow_id, payload=payload())

    assert run["status"] == "awaiting_approval"
    assert run["approval"] is not None
    assert run["approval"]["tool_name"] == CANCEL
    assert run["approval"]["step_key"] == "cancel"
    assert run["steps"][0]["status"] == "awaiting_approval"


async def test_nothing_is_cancelled_while_a_decision_is_pending(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The number that matters: zero."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())

    await start(api_client, account.headers(), workflow_id, payload=payload())

    assert await shipment_status(session, account.organization_id) is ShipmentStatus.EXCEPTION

    executions = await executions_of(session, account.organization_id)
    assert len(executions) == 1
    assert executions[0].executed is False
    assert executions[0].outcome == "approval_required"
    assert executions[0].workflow_step_run_id is not None
    assert executions[0].run_id is None


async def test_the_approval_row_links_the_run_the_step_and_the_execution(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())

    run = await start(api_client, account.headers(), workflow_id, payload=payload())

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
    assert str(approval.workflow_run_id) == run["run_id"]
    assert approval.workflow_step_run_id is not None
    assert approval.tool_execution_id is not None
    assert approval.run_id is None, "a workflow approval, not an agent one"
    assert approval.tool_name == CANCEL


async def test_a_workflow_approval_leaves_the_parameters_column_empty(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The same rule as an agent approval: arguments are business data, and an
    approval authorises an execution by its id."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())

    await start(api_client, account.headers(), workflow_id, payload=payload())

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
    assert REFERENCE not in str(approval.parameters)
    assert REASON not in str(approval.parameters)


async def test_the_queue_does_not_publish_the_arguments(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    await start(api_client, account.headers(), workflow_id, payload=payload())

    listed = await api_client.get("/api/v1/approvals", headers=account.headers())

    assert CANCEL in listed.text, "what kind of action it is"
    assert REFERENCE not in listed.text, "but not what it would do it to"
    assert REASON not in listed.text


async def test_approval_runs_the_tool_once_and_the_workflow_continues(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The whole workflow, in one test, because the parts only mean anything
    together."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    decided = await api_client.post(
        decide_url(paused["approval"]["id"], True), headers=account.headers()
    )

    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["agent_run"] is None, "no agent was involved"

    run = body["workflow_run"]
    assert run["run_id"] == paused["run_id"], "the same run, not a successor"
    assert run["status"] == "succeeded"
    assert [step["step_key"] for step in run["steps"]] == ["cancel", "confirm"]
    assert run["approval"] is None

    assert await shipment_status(session, account.organization_id) is ShipmentStatus.CANCELLED

    executions = await executions_of(session, account.organization_id)
    cancels = [e for e in executions if e.tool_name == CANCEL]
    assert len(cancels) == 1, "the same execution, updated - not a second one"
    assert cancels[0].executed is True
    assert cancels[0].outcome == "succeeded"


async def test_the_step_that_paused_keeps_its_position(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    await api_client.post(decide_url(paused["approval"]["id"], True), headers=account.headers())

    rows = (
        (
            await session.execute(
                select(WorkflowStepRun)
                .where(WorkflowStepRun.workflow_run_id == uuid.UUID(paused["run_id"]))
                .order_by(WorkflowStepRun.position)
            )
        )
        .scalars()
        .all()
    )

    assert [(row.step_key, row.position) for row in rows] == [("cancel", 1), ("confirm", 2)]
    assert rows[0].status is StepRunStatus.SUCCEEDED


async def test_the_confirming_step_sees_what_the_action_did(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The workflow's answer is what the record says, not what the tool claimed."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    run = (
        await api_client.post(decide_url(paused["approval"]["id"], True), headers=account.headers())
    ).json()["workflow_run"]

    assert run["output"]["step"] == "confirm"
    assert run["output"]["output"]["shipment"]["status"] == "cancelled"


async def test_rejection_never_runs_the_tool(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    decided = await api_client.post(
        decide_url(paused["approval"]["id"], False), headers=account.headers()
    )

    assert decided.status_code == 200, decided.text
    run = decided.json()["workflow_run"]

    assert run["status"] == "failed"
    assert run["error_code"] == "workflow_approval_rejected"
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.EXCEPTION

    executions = await executions_of(session, account.organization_id)
    assert executions[0].executed is False
    assert executions[0].outcome == "rejected"


async def test_approving_twice_does_not_cancel_twice(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())
    url = decide_url(paused["approval"]["id"], True)

    first = await api_client.post(url, headers=account.headers())
    second = await api_client.post(url, headers=account.headers())

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "approval_already_decided"

    # The confirming lookup ran too, so the count that matters is the
    # destructive one: the shipment was cancelled once.
    executions = await executions_of(session, account.organization_id)
    cancels = [e for e in executions if e.tool_name == CANCEL and e.executed]
    assert len(cancels) == 1


async def test_approving_after_a_rejection_is_refused(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    await api_client.post(decide_url(paused["approval"]["id"], False), headers=account.headers())
    late = await api_client.post(
        decide_url(paused["approval"]["id"], True), headers=account.headers()
    )

    assert late.status_code == 409
    assert await shipment_status(session, account.organization_id) is ShipmentStatus.EXCEPTION


# -- An explicit approval step ------------------------------------------------


async def test_an_approval_step_pauses_with_its_own_reason(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), gated_workflow())

    run = await start(api_client, account.headers(), workflow_id, payload=payload())

    assert run["status"] == "awaiting_approval"
    assert run["approval"]["tool_name"] is None, "it gates a decision, not a call"
    assert "agree" in run["approval"]["reason"]


async def test_approving_an_approval_step_continues_the_workflow(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), gated_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    run = (
        await api_client.post(decide_url(paused["approval"]["id"], True), headers=account.headers())
    ).json()["workflow_run"]

    assert run["status"] == "succeeded"
    assert [step["step_key"] for step in run["steps"]] == ["ask", "look"]


async def test_rejecting_without_a_declared_path_stops_the_workflow(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A process nobody designed a 'no' path for should halt, not carry on."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), gated_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    run = (
        await api_client.post(
            decide_url(paused["approval"]["id"], False), headers=account.headers()
        )
    ).json()["workflow_run"]

    assert run["status"] == "failed"
    assert run["error_code"] == "workflow_approval_rejected"
    assert len(run["steps"]) == 1


async def test_rejecting_takes_the_declared_path(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A refusal is a first-class outcome when the definition says where it
    leads - not a failure of the platform."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(
        api_client, account.headers(), gated_workflow(on_reject="declined")
    )
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    run = (
        await api_client.post(
            decide_url(paused["approval"]["id"], False), headers=account.headers()
        )
    ).json()["workflow_run"]

    assert run["status"] == "succeeded"
    assert [step["step_key"] for step in run["steps"]] == ["ask", "declined"]
    assert run["error_code"] is None, "nothing failed"


# -- Who may decide -----------------------------------------------------------


async def test_a_member_may_see_the_queue_but_not_decide(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The backend is the boundary. A hidden button is not."""
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)
    await seed(session, owner.organization_id)
    workflow_id = await published(api_client, owner.headers(), cancelling_workflow())
    paused = await start(api_client, owner.headers(), workflow_id, payload=payload())

    listed = await api_client.get("/api/v1/approvals", headers=member.headers())
    refused = await api_client.post(
        decide_url(paused["approval"]["id"], True), headers=member.headers()
    )

    assert len(listed.json()) == 1
    assert refused.status_code == 403
    assert await shipment_status(session, owner.organization_id) is ShipmentStatus.EXCEPTION


async def test_an_admin_may_decide(api_client: AsyncClient, session: AsyncSession) -> None:
    owner = await register(api_client)
    admin = await add_member(api_client, session, owner.organization_id, MemberRole.ADMIN)
    await seed(session, owner.organization_id)
    workflow_id = await published(api_client, owner.headers(), cancelling_workflow())
    paused = await start(api_client, owner.headers(), workflow_id, payload=payload())

    decided = await api_client.post(
        decide_url(paused["approval"]["id"], True), headers=admin.headers()
    )

    assert decided.status_code == 200, decided.text


async def test_another_organization_cannot_decide(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, ours.organization_id)
    workflow_id = await published(api_client, ours.headers(), cancelling_workflow())
    paused = await start(api_client, ours.headers(), workflow_id, payload=payload())

    response = await api_client.post(
        decide_url(paused["approval"]["id"], True), headers=theirs.headers()
    )

    assert response.status_code == 404, "not found, rather than forbidden"
    assert await shipment_status(session, ours.organization_id) is ShipmentStatus.EXCEPTION


async def test_an_approval_cannot_reference_another_tenants_workflow_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Refused by the database: the composite key carries the organization."""
    from sqlalchemy.exc import IntegrityError

    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, theirs.organization_id)
    workflow_id = await published(api_client, theirs.headers(), cancelling_workflow())
    paused = await start(api_client, theirs.headers(), workflow_id, payload=payload())

    session.add(
        Approval(
            organization_id=ours.organization_id,
            requested_by=ours.user_id,
            workflow_run_id=uuid.UUID(paused["run_id"]),
            action="intrude",
        )
    )

    with pytest.raises(IntegrityError) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


# -- Durability ---------------------------------------------------------------


async def test_a_paused_workflow_is_readable_later(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Somebody who closed the page comes back and finds it where they left it."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), cancelling_workflow())
    paused = await start(api_client, account.headers(), workflow_id, payload=payload())

    fetched = await api_client.get(
        f"{WORKFLOWS_URL}/{workflow_id}/runs/{paused['run_id']}", headers=account.headers()
    )

    assert fetched.json()["status"] == "awaiting_approval"
    assert fetched.json()["approval"]["id"] == paused["approval"]["id"]

    record = await session.get(WorkflowRun, uuid.UUID(paused["run_id"]))
    assert record is not None
    await session.refresh(record)
    assert record.current_step == "cancel"


# -- Two administrators at once -----------------------------------------------


async def test_two_concurrent_approvals_cancel_the_shipment_once(database: None) -> None:
    """Two administrators, two connections, one pending approval.

    Sequential double-approval is covered above; this is what the conditional
    update and the execution claim exist for. Nothing shares a transaction, so
    the two requests genuinely contend.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    def build() -> AsyncClient:
        settings = Settings(
            app_env="test", argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1
        )
        application: FastAPI = create_app(settings)

        async def own_session():  # noqa: ANN202 - a dependency override
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

    setup_client, first_client, second_client = build(), build(), build()

    account = None
    try:
        async with setup_client, first_client, second_client:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed(seeding, account.organization_id)
                await seeding.commit()

            workflow_id = await published(setup_client, account.headers(), cancelling_workflow())
            paused = await start(setup_client, account.headers(), workflow_id, payload=payload())
            assert paused["status"] == "awaiting_approval", paused

            url = decide_url(paused["approval"]["id"], True)
            first, second = await asyncio.gather(
                first_client.post(url, headers=account.headers()),
                second_client.post(url, headers=account.headers()),
            )

        assert sorted([first.status_code, second.status_code]) == [200, 409], (
            f"{first.text} / {second.text}"
        )

        async with factory() as checking:
            executions = (
                (
                    await checking.execute(
                        select(ToolExecutionRecord).where(
                            ToolExecutionRecord.organization_id == account.organization_id,
                            ToolExecutionRecord.tool_name == CANCEL,
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
