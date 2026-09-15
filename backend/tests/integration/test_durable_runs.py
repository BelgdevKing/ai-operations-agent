"""Durable runs: what survives the request, and what cannot be done twice.

Against real PostgreSQL, because every guarantee here is the database's. The
idempotency race is run with two genuinely concurrent connections rather than
two sequential calls, since a unique index that is never contended proves
nothing about what happens when it is.
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
from app.models.agent_run import AgentRunRecord, AgentStepRecord, ToolExecutionRecord
from app.models.conversation import Conversation, Message
from app.models.enums import MessageRole
from app.repositories.agent_run import ABANDONED_ERROR_CODE, AgentRunRepository
from tests.integration.agent_helpers import (
    ANSWER,
    RUN_URL,
    answering,
    asking,
    body,
    script,
    seed_shipment,
)
from tests.integration.auth_helpers import register

pytestmark = pytest.mark.integration


async def runs_of(session: AsyncSession, organization_id: uuid.UUID) -> list[AgentRunRecord]:
    result = await session.execute(
        select(AgentRunRecord)
        .where(AgentRunRecord.organization_id == organization_id)
        .order_by(AgentRunRecord.created_at)
    )
    return list(result.scalars().all())


# -- The run is written down --------------------------------------------------


async def test_a_completed_run_is_stored(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    account = await register(api_client)

    response = await api_client.post(RUN_URL, json=body(), headers=account.headers())
    assert response.status_code == 200, response.text

    stored = await runs_of(session, account.organization_id)
    assert len(stored) == 1

    record = stored[0]
    assert str(record.id) == response.json()["run_id"]
    assert record.status is AgentRunStatus.COMPLETED
    assert record.step_count == 1
    assert record.conversation_id is not None
    assert record.started_at is not None and record.completed_at is not None


async def test_each_model_call_is_stored_as_a_step(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, asking("get_shipment", shipment_reference="ABC123"), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    steps = (
        (
            await session.execute(
                select(AgentStepRecord)
                .where(AgentStepRecord.organization_id == account.organization_id)
                .order_by(AgentStepRecord.step_number)
            )
        )
        .scalars()
        .all()
    )

    assert [step.step_number for step in steps] == [1, 2]
    assert steps[0].decision_type == "tool_request"
    assert steps[0].tool_name == "get_shipment"
    assert steps[1].decision_type == "final"
    assert steps[1].tool_name is None


async def test_a_step_stores_no_conversation_content(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The operational record says a call happened; the conversation says what
    was said."""
    secret = "SHIPMENT-XY-99"
    script(app, answering("The answer mentions " + secret))
    account = await register(api_client)

    await api_client.post(RUN_URL, json=body(f"Where is {secret}?"), headers=account.headers())

    step = (
        (
            await session.execute(
                select(AgentStepRecord).where(
                    AgentStepRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .one()
    )

    assert secret not in str({c.name: getattr(step, c.name) for c in step.__table__.c})


async def test_a_tool_execution_is_stored_as_metadata_only(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Counts and codes. Never the arguments, never the returned records."""
    script(app, asking("get_shipment", shipment_reference="ABC123"), answering())
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    execution = (
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .one()
    )

    assert execution.tool_name == "get_shipment"
    assert execution.executed is True
    assert execution.outcome == "succeeded"
    assert execution.safety == "read_only"
    assert execution.argument_count == 1
    assert execution.result_field_count is not None

    columns = {column.name for column in execution.__table__.c}
    assert "arguments" not in columns
    assert "result" not in columns
    assert "ABC123" not in str({c: getattr(execution, c) for c in columns})


async def test_a_run_survives_the_request_that_made_it(
    app: FastAPI, api_client: AsyncClient
) -> None:
    """Read back through a second request, which is the boundary that matters."""
    script(app, answering())
    account = await register(api_client)

    created = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()

    fetched = await api_client.get(
        f"/api/v1/ai/runs/{created['run_id']}", headers=account.headers()
    )

    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == created["run_id"]
    assert fetched.json()["status"] == "completed"
    assert fetched.json()["final_response"] == ANSWER


async def test_a_failed_run_records_a_code_and_no_provider_text(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    # A model that only ever asks for a tool exhausts the budget.
    script(app, asking("get_shipment", shipment_reference="ABC123"))
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)

    response = await api_client.post(RUN_URL, json=body(), headers=account.headers())
    assert response.status_code == 500

    record = (await runs_of(session, account.organization_id))[0]
    assert record.status is AgentRunStatus.FAILED
    assert record.error_code == "agent_max_steps_exceeded"
    assert record.error_message == "The agent did not finish within its step limit."


# -- Idempotency --------------------------------------------------------------


async def test_the_same_key_twice_makes_one_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    account = await register(api_client)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    first = await api_client.post(RUN_URL, json=body(), headers=headers)
    second = await api_client.post(RUN_URL, json=body(), headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"]
    assert len(await runs_of(session, account.organization_id)) == 1


async def test_a_replayed_key_returns_the_original_answer(
    app: FastAPI, api_client: AsyncClient
) -> None:
    """A retry after a lost response has to give back what was already said."""
    provider = script(app, answering())
    account = await register(api_client)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    await api_client.post(RUN_URL, json=body(), headers=headers)
    replayed = await api_client.post(RUN_URL, json=body(), headers=headers)

    assert replayed.json()["final_response"] == ANSWER
    assert replayed.json()["status"] == "completed"
    assert provider.calls == 1, "the model was not asked a second time"


async def test_a_replayed_key_does_not_add_to_the_conversation(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    account = await register(api_client)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    first = (await api_client.post(RUN_URL, json=body(), headers=headers)).json()
    await api_client.post(RUN_URL, json=body(), headers=headers)

    messages = (
        (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == uuid.UUID(first["conversation_id"])
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(messages) == 2, "one question and one answer, not two of each"


async def test_a_paused_run_replays_as_awaiting_approval(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """A retry while somebody is still deciding reports the state, not a new run."""
    script(app, asking("cancel_shipment", shipment_reference="ABC123", reason="Customer asked."))
    account = await register(api_client)
    await seed_shipment(session, account.organization_id)
    headers = {**account.headers(), "Idempotency-Key": "turn-1"}

    first = (await api_client.post(RUN_URL, json=body(), headers=headers)).json()
    replayed = (await api_client.post(RUN_URL, json=body(), headers=headers)).json()

    assert first["status"] == "awaiting_approval"
    assert replayed["run_id"] == first["run_id"]
    assert replayed["status"] == "awaiting_approval"
    assert replayed["approval"]["id"] == first["approval"]["id"]


async def test_two_organizations_may_use_the_same_key(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The key is scoped to a tenant. It has to be: nobody coordinates across them."""
    script(app, answering())
    first_account = await register(api_client)
    second_account = await register(api_client)

    first = await api_client.post(
        RUN_URL, json=body(), headers={**first_account.headers(), "Idempotency-Key": "shared"}
    )
    second = await api_client.post(
        RUN_URL, json=body(), headers={**second_account.headers(), "Idempotency-Key": "shared"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["run_id"] != second.json()["run_id"]
    assert len(await runs_of(session, first_account.organization_id)) == 1
    assert len(await runs_of(session, second_account.organization_id)) == 1


async def test_runs_without_a_key_never_collide(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """NULL keys are distinct in PostgreSQL, so two plain runs are two runs."""
    script(app, answering())
    account = await register(api_client)

    first = await api_client.post(RUN_URL, json=body(), headers=account.headers())
    second = await api_client.post(RUN_URL, json=body(), headers=account.headers())

    assert first.json()["run_id"] != second.json()["run_id"]
    assert len(await runs_of(session, account.organization_id)) == 2


async def test_a_malformed_idempotency_key_is_refused(
    app: FastAPI, api_client: AsyncClient
) -> None:
    script(app, answering())
    account = await register(api_client)

    response = await api_client.post(
        RUN_URL,
        json=body(),
        headers={**account.headers(), "Idempotency-Key": "a key with spaces"},
    )

    assert response.status_code == 422


# -- The race, on real connections --------------------------------------------


def committing_client(factory: async_sessionmaker[AsyncSession]) -> AsyncClient:
    """A client whose requests each get their own session, and commit.

    Deliberately not the shared-session fixture. That fixture gives every request
    the same transaction, which is what makes the rest of the suite roll back
    cleanly - and which would also make a race impossible to observe, because two
    requests inside one transaction cannot contend for anything.
    """
    settings = Settings(
        app_env="test", argon2_time_cost=1, argon2_memory_cost_kib=8192, argon2_parallelism=1
    )
    application = create_app(settings)
    script(application, answering())

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


async def test_concurrent_requests_with_one_key_create_one_run(database: None) -> None:
    """Two requests, two connections, one key - and exactly one durable run.

    This is the test the whole idempotency design exists for, so nothing here
    shares a transaction: the two requests really do contend on
    ``UNIQUE(organization_id, idempotency_key)``, PostgreSQL really does make one
    of them wait, and the loser really does have to recover from the violation
    rather than raise it.

    The rows are committed for real, so the test removes them itself. Deleting
    the organization cascades to everything created inside it.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    first_client = committing_client(factory)
    second_client = committing_client(factory)

    account = None
    try:
        async with first_client, second_client:
            account = await register(first_client)
            headers = {**account.headers(), "Idempotency-Key": "raced"}

            first, second = await asyncio.gather(
                first_client.post(RUN_URL, json=body(), headers=headers),
                second_client.post(RUN_URL, json=body(), headers=headers),
            )

            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            assert first.json()["run_id"] == second.json()["run_id"], "one run, seen twice"

        async with factory() as checking:
            stored = (
                (
                    await checking.execute(
                        select(AgentRunRecord).where(
                            AgentRunRecord.organization_id == account.organization_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(stored) == 1, "the loser did not create a second run"
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


async def test_a_stale_running_run_is_failed_as_abandoned(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """There is no worker, so a run whose request died has to be given up on."""
    account = await register(api_client)

    record = AgentRunRecord(
        organization_id=account.organization_id,
        user_id=account.user_id,
        agent_id=uuid.uuid4(),
        status=AgentRunStatus.RUNNING,
    )
    session.add(record)
    await session.flush()

    await session.execute(
        update(AgentRunRecord)
        .where(AgentRunRecord.id == record.id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
    )

    repository = AgentRunRepository(session, account.organization_id)
    failed = await repository.sweep_abandoned(stale_after=timedelta(minutes=15))

    assert failed == 1
    await session.refresh(record)
    assert record.status is AgentRunStatus.FAILED
    assert record.error_code == ABANDONED_ERROR_CODE
    assert record.completed_at is not None


async def test_a_stale_pending_run_is_failed_too(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """It holds an idempotency key it will never use."""
    account = await register(api_client)

    record = AgentRunRecord(
        organization_id=account.organization_id,
        user_id=account.user_id,
        agent_id=uuid.uuid4(),
        status=AgentRunStatus.PENDING,
    )
    session.add(record)
    await session.flush()
    await session.execute(
        update(AgentRunRecord)
        .where(AgentRunRecord.id == record.id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
    )

    assert (
        await AgentRunRepository(session, account.organization_id).sweep_abandoned(
            stale_after=timedelta(minutes=15)
        )
        == 1
    )


async def test_a_run_awaiting_approval_is_never_swept(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """It is paused on purpose. Being old says nothing about it."""
    account = await register(api_client)

    record = AgentRunRecord(
        organization_id=account.organization_id,
        user_id=account.user_id,
        agent_id=uuid.uuid4(),
        status=AgentRunStatus.AWAITING_APPROVAL,
    )
    session.add(record)
    await session.flush()
    await session.execute(
        update(AgentRunRecord)
        .where(AgentRunRecord.id == record.id)
        .values(updated_at=datetime.now(UTC) - timedelta(days=30))
    )

    failed = await AgentRunRepository(session, account.organization_id).sweep_abandoned(
        stale_after=timedelta(minutes=15)
    )

    assert failed == 0
    await session.refresh(record)
    assert record.status is AgentRunStatus.AWAITING_APPROVAL


async def test_a_fresh_running_run_is_not_swept(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)

    session.add(
        AgentRunRecord(
            organization_id=account.organization_id,
            user_id=account.user_id,
            agent_id=uuid.uuid4(),
            status=AgentRunStatus.RUNNING,
        )
    )
    await session.flush()

    assert (
        await AgentRunRepository(session, account.organization_id).sweep_abandoned(
            stale_after=timedelta(minutes=15)
        )
        == 0
    )


async def test_the_sweep_does_not_reach_another_organization(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)

    record = AgentRunRecord(
        organization_id=theirs.organization_id,
        user_id=theirs.user_id,
        agent_id=uuid.uuid4(),
        status=AgentRunStatus.RUNNING,
    )
    session.add(record)
    await session.flush()
    await session.execute(
        update(AgentRunRecord)
        .where(AgentRunRecord.id == record.id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
    )

    failed = await AgentRunRepository(session, ours.organization_id).sweep_abandoned(
        stale_after=timedelta(minutes=15)
    )

    assert failed == 0
    await session.refresh(record)
    assert record.status is AgentRunStatus.RUNNING


# -- Tenant isolation ---------------------------------------------------------


async def test_another_organization_cannot_read_a_run(
    app: FastAPI, api_client: AsyncClient
) -> None:
    script(app, answering())
    ours = await register(api_client)
    theirs = await register(api_client)

    created = (await api_client.post(RUN_URL, json=body(), headers=ours.headers())).json()

    response = await api_client.get(
        f"/api/v1/ai/runs/{created['run_id']}", headers=theirs.headers()
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_run_not_found"


async def test_the_run_list_shows_only_this_organization(
    app: FastAPI, api_client: AsyncClient
) -> None:
    script(app, answering())
    ours = await register(api_client)
    theirs = await register(api_client)

    await api_client.post(RUN_URL, json=body(), headers=ours.headers())
    await api_client.post(RUN_URL, json=body(), headers=theirs.headers())

    listed = (await api_client.get("/api/v1/ai/runs", headers=ours.headers())).json()

    assert len(listed) == 1


async def test_a_run_cannot_be_attached_to_another_tenants_conversation(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Refused by the database, not merely filtered by the application.

    The composite foreign key carries the organization, so the row is not
    representable - which is a stronger statement than "no query returns it".
    """
    ours = await register(api_client)
    theirs = await register(api_client)

    conversation = Conversation(organization_id=theirs.organization_id, user_id=theirs.user_id)
    session.add(conversation)
    await session.flush()

    session.add(
        AgentRunRecord(
            organization_id=ours.organization_id,
            user_id=ours.user_id,
            agent_id=uuid.uuid4(),
            conversation_id=conversation.id,
        )
    )

    with pytest.raises(Exception) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


async def test_a_step_cannot_be_attached_to_another_tenants_run(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
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
        AgentStepRecord(
            organization_id=ours.organization_id,
            run_id=record.id,
            step_number=1,
            decision_type="final",
            model="test-model",
        )
    )

    with pytest.raises(Exception) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


async def test_two_steps_cannot_claim_the_same_number(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A replayed request cannot make a run look like it did more than it did."""
    account = await register(api_client)

    record = AgentRunRecord(
        organization_id=account.organization_id,
        user_id=account.user_id,
        agent_id=uuid.uuid4(),
    )
    session.add(record)
    await session.flush()

    for _ in range(2):
        session.add(
            AgentStepRecord(
                organization_id=account.organization_id,
                run_id=record.id,
                step_number=1,
                decision_type="final",
                model="test-model",
            )
        )

    with pytest.raises(Exception) as caught:
        await session.flush()

    assert "uq_agent_steps_run_id_step_number" in str(caught.value)
    await session.rollback()


async def test_the_conversation_a_run_writes_belongs_to_the_same_tenant(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    script(app, answering())
    account = await register(api_client)

    created = (await api_client.post(RUN_URL, json=body(), headers=account.headers())).json()

    conversation = await session.get(Conversation, uuid.UUID(created["conversation_id"]))
    assert conversation is not None
    assert conversation.organization_id == account.organization_id

    turns = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert [turn.role for turn in turns] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [turn.sequence for turn in turns] == [1, 2], "position, not timestamp, is the order"
