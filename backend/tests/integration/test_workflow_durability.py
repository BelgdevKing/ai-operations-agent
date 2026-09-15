"""What survives: idempotency, recovery, the audit trail, and agent steps.

The idempotency race is run on two genuinely concurrent connections, because a
unique index that is never contended proves nothing about what happens when it
is.
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

from app.agents.registry import DEMO_AGENT_ID
from app.core.config import Settings
from app.core.database import engine, get_session
from app.main import create_app
from app.models.audit import AuditEvent
from app.models.enums import RunStatus, ShipmentStatus
from app.models.workflow import WorkflowRun
from app.repositories.workflow import ABANDONED_ERROR_CODE, WorkflowRunRepository
from tests.integration.agent_helpers import answering, asking, script
from tests.integration.auth_helpers import Account, register
from tests.integration.workflow_helpers import (
    REASON,
    REFERENCE,
    WORKFLOWS_URL,
    lookup_workflow,
    published,
    seed,
    start,
    tool_step,
)

pytestmark = pytest.mark.integration


async def runs_of(session: AsyncSession, organization_id: uuid.UUID) -> list[WorkflowRun]:
    result = await session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.organization_id == organization_id)
        .order_by(WorkflowRun.created_at)
    )
    return list(result.scalars().all())


async def events_of(session: AsyncSession, organization_id: uuid.UUID) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.organization_id == organization_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return list(result.scalars().all())


# -- Idempotency --------------------------------------------------------------


async def test_the_same_key_twice_makes_one_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    first = await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")
    second = await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")

    assert first["run_id"] == second["run_id"]
    assert len(await runs_of(session, account.organization_id)) == 1


async def test_a_replayed_key_returns_the_original_result(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")
    replayed = await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")

    assert replayed["status"] == "succeeded"
    assert replayed["step_count"] == 2
    assert replayed["output"] is not None


async def test_a_replay_runs_no_step_a_second_time(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    from app.models.agent_run import ToolExecutionRecord

    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")
    await start(api_client, account.headers(), workflow_id, idempotency_key="turn-1")

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

    assert len(executions) == 2, "two steps, once each"


async def test_two_organizations_may_use_the_same_key(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, ours.organization_id)
    await seed(session, theirs.organization_id)

    our_workflow = await published(api_client, ours.headers(), lookup_workflow())
    their_workflow = await published(api_client, theirs.headers(), lookup_workflow())

    first = await start(api_client, ours.headers(), our_workflow, idempotency_key="shared")
    second = await start(api_client, theirs.headers(), their_workflow, idempotency_key="shared")

    assert first["run_id"] != second["run_id"]
    assert len(await runs_of(session, ours.organization_id)) == 1
    assert len(await runs_of(session, theirs.organization_id)) == 1


async def test_runs_without_a_key_never_collide(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    first = await start(api_client, account.headers(), workflow_id)
    second = await start(api_client, account.headers(), workflow_id)

    assert first["run_id"] != second["run_id"]
    assert len(await runs_of(session, account.organization_id)) == 2


async def test_a_malformed_key_is_refused(api_client: AsyncClient) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs",
        json={"input": {}},
        headers={**account.headers(), "Idempotency-Key": "a key with spaces"},
    )

    assert response.status_code == 422


def committing_client(factory: async_sessionmaker[AsyncSession]) -> AsyncClient:
    """A client whose requests each get their own session, and commit.

    Deliberately not the shared-session fixture: two requests inside one
    transaction cannot contend for anything, which would make a race impossible
    to observe.
    """
    settings = Settings(
        app_env="test", argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1
    )
    application: FastAPI = create_app(settings)

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


async def test_concurrent_starts_with_one_key_create_one_run(database: None) -> None:
    """Two requests, two connections, one key - and exactly one durable run."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    setup_client = committing_client(factory)
    first_client = committing_client(factory)
    second_client = committing_client(factory)

    account = None
    try:
        async with setup_client, first_client, second_client:
            account = await register(setup_client)

            async with factory() as seeding:
                await seed(seeding, account.organization_id)
                await seeding.commit()

            workflow_id = await published(setup_client, account.headers(), lookup_workflow())
            headers = {**account.headers(), "Idempotency-Key": "raced"}
            body = {"input": {"shipment_reference": REFERENCE}}
            url = f"{WORKFLOWS_URL}/{workflow_id}/runs"

            first, second = await asyncio.gather(
                first_client.post(url, json=body, headers=headers),
                second_client.post(url, json=body, headers=headers),
            )

            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            assert first.json()["run_id"] == second.json()["run_id"], "one run, seen twice"

        async with factory() as checking:
            stored = await runs_of(checking, account.organization_id)
            assert len(stored) == 1, "the loser did not create a second run"
            assert stored[0].status is RunStatus.SUCCEEDED
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


# -- Recovery -----------------------------------------------------------------


async def stale_run(
    session: AsyncSession,
    account: Account,
    status: RunStatus,
    workflow_id: uuid.UUID,
) -> WorkflowRun:
    """A run whose record has not moved for two hours."""
    record = WorkflowRun(
        organization_id=account.organization_id,
        workflow_id=workflow_id,
        workflow_version=1,
        user_id=account.user_id,
        status=status,
    )
    session.add(record)
    await session.flush()
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == record.id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
        .execution_options(synchronize_session=False)
    )
    return record


async def test_a_stale_running_run_is_failed_as_abandoned(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """There is no worker, so a run whose request died has to be given up on."""
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    record = await stale_run(session, account, RunStatus.RUNNING, uuid.UUID(workflow_id))

    failed = await WorkflowRunRepository(session, account.organization_id).sweep_abandoned(
        stale_after=timedelta(minutes=15)
    )

    assert failed == 1
    await session.refresh(record)
    assert record.status is RunStatus.FAILED
    assert record.error_code == ABANDONED_ERROR_CODE
    assert record.completed_at is not None


async def test_a_stale_pending_run_is_failed_too(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    await stale_run(session, account, RunStatus.PENDING, uuid.UUID(workflow_id))

    assert (
        await WorkflowRunRepository(session, account.organization_id).sweep_abandoned(
            stale_after=timedelta(minutes=15)
        )
        == 1
    )


async def test_a_run_awaiting_approval_is_never_swept(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """It is paused on purpose. Being old says nothing about it."""
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    record = await stale_run(session, account, RunStatus.AWAITING_APPROVAL, uuid.UUID(workflow_id))

    failed = await WorkflowRunRepository(session, account.organization_id).sweep_abandoned(
        stale_after=timedelta(minutes=15)
    )

    assert failed == 0
    await session.refresh(record)
    assert record.status is RunStatus.AWAITING_APPROVAL


async def test_a_fresh_running_run_is_not_swept(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    session.add(
        WorkflowRun(
            organization_id=account.organization_id,
            workflow_id=uuid.UUID(workflow_id),
            workflow_version=1,
            user_id=account.user_id,
            status=RunStatus.RUNNING,
        )
    )
    await session.flush()

    assert (
        await WorkflowRunRepository(session, account.organization_id).sweep_abandoned(
            stale_after=timedelta(minutes=15)
        )
        == 0
    )


async def test_the_sweep_does_not_reach_another_organization(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    workflow_id = await published(api_client, theirs.headers(), lookup_workflow())
    record = await stale_run(session, theirs, RunStatus.RUNNING, uuid.UUID(workflow_id))

    failed = await WorkflowRunRepository(session, ours.organization_id).sweep_abandoned(
        stale_after=timedelta(minutes=15)
    )

    assert failed == 0
    await session.refresh(record)
    assert record.status is RunStatus.RUNNING


async def test_starting_a_run_sweeps_this_organizations_abandoned_ones(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The sweep runs on the way in, because there is no timer to run it on."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    abandoned = await stale_run(session, account, RunStatus.RUNNING, uuid.UUID(workflow_id))
    await session.commit()

    await start(api_client, account.headers(), workflow_id)

    await session.refresh(abandoned)
    assert abandoned.status is RunStatus.FAILED
    assert abandoned.error_code == ABANDONED_ERROR_CODE


# -- Audit --------------------------------------------------------------------


async def test_creation_and_outcome_are_both_recorded(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    events = await events_of(session, account.organization_id)
    assert [event.event_type for event in events] == ["workflow.run.created", "workflow.run"]
    assert [event.action for event in events] == ["pending", "succeeded"]
    assert all(str(event.resource_id) == run["run_id"] for event in events)
    assert all(event.resource_type == "workflow_run" for event in events)


async def test_the_audit_records_which_version_ran(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(api_client, account.headers(), workflow_id)

    events = await events_of(session, account.organization_id)
    assert all(event.event_metadata["workflow_version"] == 1 for event in events)
    assert all(event.event_metadata["workflow_id"] == workflow_id for event in events)


async def test_a_failure_is_recorded_with_its_code(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(api_client, account.headers(), workflow_id)

    finished = next(
        event
        for event in await events_of(session, account.organization_id)
        if event.event_type == "workflow.run"
    )

    assert finished.action == "failed"
    assert finished.event_metadata["error_code"] == "shipment_not_found"


async def test_no_audit_row_holds_the_input_or_the_output(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """An audit trail is read by more people than any other table here."""
    secret = "WF-SENSITIVE-001"
    account = await register(api_client)
    await seed(session, account.organization_id, reference=secret)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(
        api_client,
        account.headers(),
        workflow_id,
        payload={"shipment_reference": secret, "note": REASON},
        idempotency_key="private-key-4471",
    )

    recorded = str(
        [
            (event.event_type, event.action, event.event_metadata)
            for event in await events_of(session, account.organization_id)
        ]
    )

    assert secret not in recorded, "not the input"
    assert REASON not in recorded
    assert "Rotterdam" not in recorded, "not what a tool returned"
    assert "private-key-4471" not in recorded, "not the client's opaque token"


async def test_a_paused_workflow_records_the_approval_request(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(
        api_client,
        account.headers(),
        {
            "entry": "cancel",
            "steps": [
                tool_step(
                    "cancel",
                    "cancel_shipment",
                    arguments={
                        "shipment_reference": "$.input.shipment_reference",
                        "reason": "$.input.reason",
                    },
                    next_step=None,
                )
            ],
        },
    )

    run = await start(
        api_client,
        account.headers(),
        workflow_id,
        payload={"shipment_reference": REFERENCE, "reason": REASON},
    )

    events = await events_of(session, account.organization_id)
    assert [event.event_type for event in events] == [
        "workflow.run.created",
        "workflow.run",
        "approval.requested",
    ]
    requested = events[2]
    assert requested.event_metadata["workflow_run_id"] == run["run_id"]
    assert requested.event_metadata["tool_name"] == "cancel_shipment"


async def test_the_whole_story_of_an_approved_workflow_is_in_the_trail(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(
        api_client,
        account.headers(),
        {
            "entry": "cancel",
            "steps": [
                tool_step(
                    "cancel",
                    "cancel_shipment",
                    arguments={
                        "shipment_reference": "$.input.shipment_reference",
                        "reason": "$.input.reason",
                    },
                    next_step=None,
                )
            ],
        },
    )
    paused = await start(
        api_client,
        account.headers(),
        workflow_id,
        payload={"shipment_reference": REFERENCE, "reason": REASON},
    )

    await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
    )

    types = [event.event_type for event in await events_of(session, account.organization_id)]

    assert types == [
        "workflow.run.created",
        "workflow.run",
        "approval.requested",
        "approval.approved",
        "workflow.run",
    ]


# -- Agent steps --------------------------------------------------------------


def agent_workflow() -> dict[str, object]:
    return {
        "entry": "ask",
        "steps": [
            {
                "id": "ask",
                "type": "agent_step",
                "agent_id": str(DEMO_AGENT_ID),
                "message": "$.input.question",
                "next": None,
            }
        ],
    }


async def test_an_agent_step_runs_through_the_existing_runtime(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The workflow names an agent. It never learns which provider answered."""
    provider = script(app, answering("The shipment is in exception."))
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), agent_workflow())

    run = await start(
        api_client, account.headers(), workflow_id, payload={"question": "Where is ABC123?"}
    )

    assert run["status"] == "succeeded"
    assert provider.calls == 1
    assert run["output"]["output"]["response"] == "The shipment is in exception."
    assert "provider" not in str(run) and "anthropic" not in str(run).lower()


async def test_an_agent_step_links_the_agent_run_it_made(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """One request id leads to the workflow run, to the step, to the agent run."""
    from app.models.workflow import WorkflowStepRun

    script(app, answering("Done."))
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), agent_workflow())

    run = await start(api_client, account.headers(), workflow_id, payload={"question": "Hi"})

    step = (
        (
            await session.execute(
                select(WorkflowStepRun).where(
                    WorkflowStepRun.workflow_run_id == uuid.UUID(run["run_id"])
                )
            )
        )
        .scalars()
        .one()
    )

    assert step.agent_run_id is not None
    assert step.organization_id == account.organization_id


async def test_an_agent_step_that_pauses_pauses_the_workflow(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The agent stopped for its own tool approval, and the workflow waits with
    it - on the same approval row, so one decision resumes both."""
    script(
        app,
        asking("cancel_shipment", shipment_reference=REFERENCE, reason=REASON),
        answering("It has been cancelled."),
    )
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.IN_TRANSIT)
    workflow_id = await published(api_client, account.headers(), agent_workflow())

    run = await start(
        api_client, account.headers(), workflow_id, payload={"question": "Cancel it."}
    )

    assert run["status"] == "awaiting_approval"
    assert run["approval"] is not None
    assert run["approval"]["tool_name"] == "cancel_shipment"


async def test_deciding_resumes_the_agent_and_then_the_workflow(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(
        app,
        asking("cancel_shipment", shipment_reference=REFERENCE, reason=REASON),
        answering("It has been cancelled."),
    )
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.IN_TRANSIT)
    workflow_id = await published(api_client, account.headers(), agent_workflow())
    paused = await start(
        api_client, account.headers(), workflow_id, payload={"question": "Cancel it."}
    )

    decided = await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
    )

    assert decided.status_code == 200, decided.text
    body = decided.json()

    assert body["agent_run"] is not None, "the agent run resumed"
    assert body["agent_run"]["status"] == "completed"
    assert body["workflow_run"] is not None, "and then the workflow did"
    assert body["workflow_run"]["run_id"] == paused["run_id"]
    assert body["workflow_run"]["status"] == "succeeded"
