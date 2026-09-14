"""The schema as PostgreSQL actually has it, plus constraint enforcement.

Skipped when PostgreSQL is unreachable. Every test runs inside a transaction
that is rolled back, so nothing persists.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, AuditEvent, Base, Document, Organization, User, WorkflowStep
from app.models.enums import MemberRole, WorkflowStepType
from tests.integration.factories import (
    make_agent,
    make_member,
    make_organization,
    make_user,
    make_workflow,
)

pytestmark = pytest.mark.integration

EXPECTED_TABLES = set(Base.metadata.tables)


# -- The tables exist ---------------------------------------------------------


async def test_every_model_table_exists_in_postgresql(session: AsyncSession) -> None:
    def read(connection: Connection) -> set[str]:
        return set(inspect(connection).get_table_names(schema="public"))

    present = await session.connection()
    actual = await present.run_sync(read)

    missing = EXPECTED_TABLES - actual
    assert not missing, f"tables declared but not migrated: {sorted(missing)}"


async def test_alembic_version_is_recorded(session: AsyncSession) -> None:
    version = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()

    assert version, "the database must be stamped with a migration revision"


async def test_timestamps_are_stored_with_a_time_zone(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT data_type, count(*) FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%' "
                "GROUP BY data_type"
            )
        )
    ).all()

    assert [row[0] for row in rows] == ["timestamp with time zone"]


async def test_jsonb_columns_are_really_jsonb(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND udt_name = 'jsonb' "
                "ORDER BY table_name, column_name"
            )
        )
    ).all()

    assert {(t, c) for t, c in rows} == {
        ("approvals", "parameters"),
        ("audit_events", "metadata"),
        ("workflow_step_runs", "input_data"),
        ("workflow_step_runs", "output_data"),
        ("workflow_steps", "configuration"),
    }


async def test_uuid_primary_keys_are_uuid_typed(session: AsyncSession) -> None:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "WHERE c.table_schema = 'public' AND c.column_name = 'id' "
                    "AND c.udt_name <> 'uuid'"
                )
            )
        )
        .scalars()
        .all()
    )

    assert not rows, f"non-UUID id columns: {rows}"


# -- Identity -----------------------------------------------------------------


async def test_primary_key_is_generated_on_insert(session: AsyncSession) -> None:
    organization = await make_organization(session)

    assert isinstance(organization.id, uuid.UUID)


async def test_timestamps_are_populated_by_the_database(session: AsyncSession) -> None:
    organization = await make_organization(session)
    await session.refresh(organization)

    assert organization.created_at is not None
    assert organization.created_at.tzinfo is not None
    assert organization.updated_at is not None


# -- Unique constraints -------------------------------------------------------


async def test_organization_slug_must_be_unique(session: AsyncSession) -> None:
    await make_organization(session, slug="duplicate-slug")

    with pytest.raises(IntegrityError):
        await make_organization(session, slug="duplicate-slug")
    await session.rollback()


async def test_user_email_must_be_unique(session: AsyncSession) -> None:
    await make_user(session, email="taken@example.test")

    with pytest.raises(IntegrityError):
        await make_user(session, email="taken@example.test")
    await session.rollback()


async def test_a_user_cannot_join_the_same_organization_twice(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    await make_member(session, organization, user, MemberRole.MEMBER)

    with pytest.raises(IntegrityError):
        await make_member(session, organization, user, MemberRole.ADMIN)
    await session.rollback()


async def test_agent_names_are_unique_inside_an_organization(session: AsyncSession) -> None:
    organization = await make_organization(session)
    await make_agent(session, organization, name="Support")

    with pytest.raises(IntegrityError):
        await make_agent(session, organization, name="Support")
    await session.rollback()


async def test_agent_names_may_repeat_across_organizations(session: AsyncSession) -> None:
    """Tenants must not be able to collide with each other's naming."""
    first = await make_organization(session)
    second = await make_organization(session)

    await make_agent(session, first, name="Support")
    await make_agent(session, second, name="Support")  # must not raise


async def test_workflow_step_order_is_unique_within_a_workflow(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)

    session.add(
        WorkflowStep(
            workflow_id=workflow.id, name="A", step_type=WorkflowStepType.TOOL_CALL, step_order=1
        )
    )
    await session.flush()

    session.add(
        WorkflowStep(
            workflow_id=workflow.id, name="B", step_type=WorkflowStepType.TOOL_CALL, step_order=1
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# -- Foreign keys -------------------------------------------------------------


async def test_a_tenant_row_cannot_reference_a_missing_organization(
    session: AsyncSession,
) -> None:
    session.add(Agent(organization_id=uuid.uuid4(), name="Orphan", model="claude-sonnet-5"))

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_organization_id_cannot_be_null(session: AsyncSession) -> None:
    """Tenant ownership is not optional."""
    session.add(Agent(organization_id=None, name="No tenant", model="claude-sonnet-5"))

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# -- Check constraints --------------------------------------------------------


async def test_an_unknown_status_is_rejected(session: AsyncSession) -> None:
    """The enum check constraint is enforced by the database, not only by
    Python."""
    organization = await make_organization(session)

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.execute(
            text("UPDATE organizations SET status = 'not-a-status' WHERE id = :id"),
            {"id": organization.id},
        )
    await session.rollback()


async def test_document_size_cannot_be_negative(session: AsyncSession) -> None:
    organization = await make_organization(session)
    session.add(
        Document(
            organization_id=organization.id,
            filename="report.pdf",
            content_type="application/pdf",
            storage_key=f"docs/{uuid.uuid4()}",
            size_bytes=-1,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_workflow_step_order_cannot_be_negative(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    session.add(
        WorkflowStep(
            workflow_id=workflow.id,
            name="Bad",
            step_type=WorkflowStepType.TOOL_CALL,
            step_order=-1,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# -- Defaults -----------------------------------------------------------------


async def test_status_defaults_are_applied(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    await session.refresh(organization)
    await session.refresh(user)

    assert organization.status == "active"
    assert user.status == "pending"


async def test_jsonb_defaults_to_an_empty_object(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    step = WorkflowStep(
        workflow_id=workflow.id, name="Step", step_type=WorkflowStepType.CONDITION, step_order=0
    )
    session.add(step)
    await session.flush()
    await session.refresh(step)

    assert step.configuration == {}


async def test_audit_metadata_round_trips_as_jsonb(session: AsyncSession) -> None:
    organization = await make_organization(session)
    payload = {"ip": "10.0.0.1", "changes": {"status": ["active", "suspended"]}, "count": 3}

    event = AuditEvent(
        organization_id=organization.id,
        event_type="organization.updated",
        action="update",
        event_metadata=payload,
    )
    session.add(event)
    await session.flush()
    await session.refresh(event)

    assert event.event_metadata == payload
    # Queryable as JSON, which is the reason for JSONB over TEXT.
    found = (
        await session.execute(
            text("SELECT metadata->>'ip' FROM audit_events WHERE id = :id"), {"id": event.id}
        )
    ).scalar_one()
    assert found == "10.0.0.1"


# -- Cascades -----------------------------------------------------------------


async def test_deleting_an_organization_removes_its_tenant_data(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    await make_member(session, organization, user)
    await make_agent(session, organization)
    await session.commit()

    await session.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": organization.id})

    for table in ("organization_members", "agents"):
        remaining = (
            await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE organization_id = :id"),
                {"id": organization.id},
            )
        ).scalar_one()
        assert remaining == 0, table

    # The user is global and survives its organization.
    assert await session.get(User, user.id) is not None


async def test_a_user_with_conversations_cannot_be_deleted(session: AsyncSession) -> None:
    """RESTRICT protects the record of what was asked and done."""
    from tests.integration.factories import make_conversation

    organization = await make_organization(session)
    user = await make_user(session)
    agent = await make_agent(session, organization)
    await make_conversation(session, organization, user, agent)
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        await session.flush()
    await session.rollback()


async def test_removing_an_uploader_keeps_the_document(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    document = Document(
        organization_id=organization.id,
        uploaded_by=user.id,
        filename="report.pdf",
        content_type="application/pdf",
        storage_key=f"docs/{uuid.uuid4()}",
        size_bytes=1024,
    )
    session.add(document)
    await session.commit()

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    await session.refresh(document)

    assert document.uploaded_by is None
    assert await session.get(Document, document.id) is not None


async def test_organizations_are_isolated_by_their_tenant_column(session: AsyncSession) -> None:
    """The predicate every scoped repository will apply actually separates
    two tenants' rows."""
    first = await make_organization(session)
    second = await make_organization(session)
    await make_agent(session, first, name="Shared name")
    await make_agent(session, second, name="Shared name")
    await session.flush()

    from sqlalchemy import select

    rows = (
        (await session.execute(select(Agent).where(Agent.organization_id == first.id)))
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert all(agent.organization_id == first.id for agent in rows)


async def test_organization_relationship_is_navigable(session: AsyncSession) -> None:
    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    await session.refresh(agent, ["organization"])

    assert isinstance(agent.organization, Organization)
    assert agent.organization.id == organization.id
