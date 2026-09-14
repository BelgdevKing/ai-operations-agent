"""Relationships exercised against a real database.

Skipped when PostgreSQL is unreachable. Every test runs inside a transaction
that is rolled back, so nothing persists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Agent,
    AgentTool,
    Approval,
    AuditEvent,
    Message,
    Organization,
    Tool,
    User,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
)
from app.models.enums import (
    ApprovalStatus,
    MemberRole,
    MessageRole,
    RunStatus,
    StepRunStatus,
    WorkflowStepType,
)
from tests.integration.factories import (
    make_agent,
    make_conversation,
    make_member,
    make_organization,
    make_run,
    make_step,
    make_tool,
    make_user,
    make_workflow,
)

pytestmark = pytest.mark.integration


# -- Organization and membership ---------------------------------------------


async def test_organization_lists_its_members(session: AsyncSession) -> None:
    organization = await make_organization(session)
    owner = await make_user(session)
    member = await make_user(session)
    await make_member(session, organization, owner, MemberRole.OWNER)
    await make_member(session, organization, member, MemberRole.MEMBER)

    await session.refresh(organization, ["members"])

    assert len(organization.members) == 2
    assert {m.role for m in organization.members} == {MemberRole.OWNER, MemberRole.MEMBER}


async def test_a_user_belongs_to_several_organizations_with_different_roles(
    session: AsyncSession,
) -> None:
    """The reason role lives on the membership and not on the user."""
    user = await make_user(session)
    first = await make_organization(session)
    second = await make_organization(session)
    await make_member(session, first, user, MemberRole.OWNER)
    await make_member(session, second, user, MemberRole.MEMBER)

    await session.refresh(user, ["memberships"])

    assert len(user.memberships) == 2
    by_org = {m.organization_id: m.role for m in user.memberships}
    assert by_org[first.id] == MemberRole.OWNER
    assert by_org[second.id] == MemberRole.MEMBER


async def test_membership_navigates_to_both_sides(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    membership = await make_member(session, organization, user, MemberRole.ADMIN)

    await session.refresh(membership, ["organization", "user"])

    assert isinstance(membership.organization, Organization)
    assert isinstance(membership.user, User)
    assert membership.organization.id == organization.id
    assert membership.user.id == user.id


async def test_every_member_role_is_accepted(session: AsyncSession) -> None:
    organization = await make_organization(session)

    for role in (MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MEMBER):
        user = await make_user(session)
        membership = await make_member(session, organization, user, role)
        await session.refresh(membership)
        assert membership.role == role


# -- Agents and tools ---------------------------------------------------------


async def test_an_agent_is_granted_several_tools(session: AsyncSession) -> None:
    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    search = await make_tool(session)
    refund = await make_tool(session, requires_approval=True)

    # Load the collection before mutating it: async SQLAlchemy cannot emit
    # the lazy SELECT that plain attribute access would trigger.
    await session.refresh(agent, ["tools"])
    agent.tools.append(search)
    agent.tools.append(refund)
    await session.flush()
    await session.refresh(agent, ["tools"])

    assert {tool.id for tool in agent.tools} == {search.id, refund.id}


async def test_a_tool_is_shared_by_agents_in_different_organizations(
    session: AsyncSession,
) -> None:
    """Tools are a platform-level registry, not tenant-owned."""
    tool = await make_tool(session)
    first = await make_agent(session, await make_organization(session))
    second = await make_agent(session, await make_organization(session))

    await session.refresh(first, ["tools"])
    await session.refresh(second, ["tools"])
    first.tools.append(tool)
    second.tools.append(tool)
    await session.flush()
    await session.refresh(tool, ["agents"])

    assert {agent.id for agent in tool.agents} == {first.id, second.id}


async def test_granting_the_same_tool_twice_is_impossible(session: AsyncSession) -> None:
    """The composite primary key, rather than a separate unique constraint."""
    from sqlalchemy.exc import IntegrityError

    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    tool = await make_tool(session)
    session.add(AgentTool(agent_id=agent.id, tool_id=tool.id))
    await session.flush()

    session.add(AgentTool(agent_id=agent.id, tool_id=tool.id))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_the_grant_records_when_it_was_made(session: AsyncSession) -> None:
    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    tool = await make_tool(session)
    grant = AgentTool(agent_id=agent.id, tool_id=tool.id)
    session.add(grant)
    await session.flush()
    await session.refresh(grant)

    assert grant.created_at is not None
    assert grant.created_at.tzinfo is not None


async def test_revoking_an_agent_removes_its_grants_but_not_the_tool(
    session: AsyncSession,
) -> None:
    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    tool = await make_tool(session)
    await session.refresh(agent, ["tools"])
    agent.tools.append(tool)
    await session.commit()

    await session.delete(agent)
    await session.flush()

    remaining = (
        (await session.execute(select(AgentTool).where(AgentTool.tool_id == tool.id)))
        .scalars()
        .all()
    )
    assert remaining == []
    assert await session.get(Tool, tool.id) is not None


# -- Conversations and messages ----------------------------------------------


async def test_a_conversation_keeps_its_messages_in_order(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    agent = await make_agent(session, organization)
    conversation = await make_conversation(session, organization, user, agent)

    # Explicit timestamps: now() is constant for a whole transaction, so the
    # database default could not distinguish these.
    base = datetime.now(UTC)
    for offset, (role, content) in enumerate(
        [
            (MessageRole.SYSTEM, "You are a helpful operations agent."),
            (MessageRole.USER, "How many refunds are pending?"),
            (MessageRole.ASSISTANT, "Checking now."),
            (MessageRole.TOOL, '{"pending": 4}'),
        ]
    ):
        session.add(
            Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                created_at=base + timedelta(seconds=offset),
            )
        )
    await session.flush()
    await session.refresh(conversation, ["messages"])

    assert [m.role for m in conversation.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]


async def test_a_message_navigates_back_to_its_conversation(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    agent = await make_agent(session, organization)
    conversation = await make_conversation(session, organization, user, agent, title="Refunds")
    message = Message(conversation_id=conversation.id, role=MessageRole.USER, content="Hello")
    session.add(message)
    await session.flush()
    await session.refresh(message, ["conversation"])

    assert message.conversation.id == conversation.id
    assert message.conversation.title == "Refunds"


async def test_deleting_a_conversation_deletes_its_messages(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    agent = await make_agent(session, organization)
    conversation = await make_conversation(session, organization, user, agent)
    session.add(Message(conversation_id=conversation.id, role=MessageRole.USER, content="Hi"))
    await session.commit()

    await session.delete(conversation)
    await session.flush()

    remaining = (
        (await session.execute(select(Message).where(Message.conversation_id == conversation.id)))
        .scalars()
        .all()
    )
    assert remaining == []


async def test_a_conversation_reaches_its_agent_and_user(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)
    agent = await make_agent(session, organization)
    conversation = await make_conversation(session, organization, user, agent)

    await session.refresh(conversation, ["agent", "user", "organization"])

    assert conversation.agent.id == agent.id
    assert conversation.user.id == user.id
    assert conversation.organization.id == organization.id


# -- Workflows ----------------------------------------------------------------


async def test_a_workflow_keeps_its_steps_in_declared_order(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    await make_step(session, workflow, 2, WorkflowStepType.APPROVAL, name="Approve")
    await make_step(session, workflow, 0, WorkflowStepType.AGENT_STEP, name="Draft")
    await make_step(session, workflow, 1, WorkflowStepType.CONDITION, name="Check")

    await session.refresh(workflow, ["steps"])

    assert [step.step_order for step in workflow.steps] == [0, 1, 2]
    assert [step.name for step in workflow.steps] == ["Draft", "Check", "Approve"]


async def test_step_configuration_stores_structured_json(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    configuration = {"tool": "issue_refund", "arguments": {"max_amount": 500}, "retries": 2}
    step = await make_step(session, workflow, 0, configuration=configuration)

    await session.refresh(step)

    assert step.configuration == configuration
    assert step.configuration["arguments"]["max_amount"] == 500


async def test_a_run_links_to_its_workflow_and_step_runs(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    step = await make_step(session, workflow, 0)
    run = await make_run(session, workflow, organization, status=RunStatus.RUNNING)

    session.add(
        WorkflowStepRun(
            workflow_run_id=run.id,
            workflow_step_id=step.id,
            status=StepRunStatus.SUCCEEDED,
            input_data={"query": "pending refunds"},
            output_data={"count": 4},
        )
    )
    await session.flush()
    await session.refresh(run, ["workflow", "step_runs"])

    assert run.workflow.id == workflow.id
    assert len(run.step_runs) == 1
    assert run.step_runs[0].output_data == {"count": 4}


async def test_step_run_data_distinguishes_absent_from_empty(session: AsyncSession) -> None:
    """A resumed run needs to tell 'not started' from 'produced nothing'."""
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    step = await make_step(session, workflow, 0)
    run = await make_run(session, workflow, organization)

    pending = WorkflowStepRun(workflow_run_id=run.id, workflow_step_id=step.id)
    finished = WorkflowStepRun(
        workflow_run_id=run.id,
        workflow_step_id=step.id,
        status=StepRunStatus.SUCCEEDED,
        output_data={},
    )
    session.add_all([pending, finished])
    await session.flush()
    await session.refresh(pending)
    await session.refresh(finished)

    assert pending.output_data is None
    assert finished.output_data == {}


async def test_a_step_may_be_retried_within_one_run(session: AsyncSession) -> None:
    """Attempts are separate rows, so the history stays complete."""
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    step = await make_step(session, workflow, 0)
    run = await make_run(session, workflow, organization)

    session.add_all(
        [
            WorkflowStepRun(
                workflow_run_id=run.id,
                workflow_step_id=step.id,
                status=StepRunStatus.FAILED,
                error_message="timeout",
            ),
            WorkflowStepRun(
                workflow_run_id=run.id,
                workflow_step_id=step.id,
                status=StepRunStatus.SUCCEEDED,
            ),
        ]
    )
    await session.flush()
    await session.refresh(run, ["step_runs"])

    assert len(run.step_runs) == 2


async def test_deleting_a_workflow_removes_its_steps_and_runs(session: AsyncSession) -> None:
    organization = await make_organization(session)
    workflow = await make_workflow(session, organization)
    step = await make_step(session, workflow, 0)
    run = await make_run(session, workflow, organization)
    session.add(WorkflowStepRun(workflow_run_id=run.id, workflow_step_id=step.id))
    await session.commit()

    await session.delete(workflow)
    await session.flush()

    for model, column, value in (
        (WorkflowStep, WorkflowStep.workflow_id, workflow.id),
        (WorkflowRun, WorkflowRun.workflow_id, workflow.id),
        (WorkflowStepRun, WorkflowStepRun.workflow_run_id, run.id),
    ):
        remaining = (await session.execute(select(model).where(column == value))).scalars().all()
        assert remaining == [], model.__name__


# -- Approvals ----------------------------------------------------------------


async def test_an_approval_links_every_party_to_the_action(session: AsyncSession) -> None:
    organization = await make_organization(session)
    requester = await make_user(session)
    approver = await make_user(session)
    agent = await make_agent(session, organization)
    tool = await make_tool(session, requires_approval=True)
    conversation = await make_conversation(session, organization, requester, agent)

    approval = Approval(
        organization_id=organization.id,
        requested_by=requester.id,
        agent_id=agent.id,
        tool_id=tool.id,
        conversation_id=conversation.id,
        action="issue_refund",
        parameters={"order_id": "A-1", "amount": 250},
        reason="Refund over the automatic limit",
        approved_by=approver.id,
        status=ApprovalStatus.APPROVED,
        approved_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(approval)
    await session.flush()
    await session.refresh(
        approval, ["requester", "approver", "agent", "tool", "conversation", "organization"]
    )

    assert approval.requester.id == requester.id
    assert approval.approver is not None
    assert approval.approver.id == approver.id
    assert approval.agent is not None
    assert approval.agent.id == agent.id
    assert approval.tool is not None
    assert approval.tool.id == tool.id
    assert approval.conversation is not None
    assert approval.conversation.id == conversation.id
    assert approval.organization.id == organization.id


async def test_an_approval_preserves_the_exact_arguments(session: AsyncSession) -> None:
    """Approving replays the original call rather than re-deriving it."""
    organization = await make_organization(session)
    requester = await make_user(session)
    parameters = {"order_id": "A-1", "amount": 250.5, "notify": True, "tags": ["urgent"]}

    approval = Approval(
        organization_id=organization.id,
        requested_by=requester.id,
        action="issue_refund",
        parameters=parameters,
    )
    session.add(approval)
    await session.flush()
    await session.refresh(approval)

    assert approval.parameters == parameters


async def test_an_approval_need_not_come_from_a_conversation(session: AsyncSession) -> None:
    """A workflow step can raise one too, so the links are optional."""
    organization = await make_organization(session)
    requester = await make_user(session)

    approval = Approval(
        organization_id=organization.id,
        requested_by=requester.id,
        action="deploy",
    )
    session.add(approval)
    await session.flush()
    await session.refresh(approval)

    assert approval.agent_id is None
    assert approval.conversation_id is None
    assert approval.tool_id is None
    assert approval.status == ApprovalStatus.PENDING
    assert approval.requested_at is not None


async def test_an_unapproved_request_has_no_approver(session: AsyncSession) -> None:
    organization = await make_organization(session)
    requester = await make_user(session)
    approval = Approval(
        organization_id=organization.id, requested_by=requester.id, action="delete_account"
    )
    session.add(approval)
    await session.flush()
    await session.refresh(approval, ["approver"])

    assert approval.approver is None
    assert approval.approved_at is None


# -- Audit --------------------------------------------------------------------


async def test_an_audit_event_links_to_its_tenant_and_actor(session: AsyncSession) -> None:
    organization = await make_organization(session)
    user = await make_user(session)

    event = AuditEvent(
        organization_id=organization.id,
        user_id=user.id,
        event_type="agent.updated",
        resource_type="agent",
        resource_id=uuid.uuid4(),
        action="update",
        event_metadata={"field": "system_instructions"},
    )
    session.add(event)
    await session.flush()
    await session.refresh(event, ["organization", "user"])

    assert event.organization.id == organization.id
    assert event.user is not None
    assert event.user.id == user.id


async def test_a_system_event_has_no_user(session: AsyncSession) -> None:
    """An agent or a scheduled job acts without a person behind it."""
    organization = await make_organization(session)
    event = AuditEvent(
        organization_id=organization.id,
        event_type="workflow.run.completed",
        action="complete",
    )
    session.add(event)
    await session.flush()
    await session.refresh(event, ["user"])

    assert event.user is None
    assert event.event_metadata == {}


async def test_the_audit_trail_survives_the_row_it_describes(session: AsyncSession) -> None:
    """resource_id is deliberately not a foreign key."""
    organization = await make_organization(session)
    agent = await make_agent(session, organization)
    event = AuditEvent(
        organization_id=organization.id,
        event_type="agent.deleted",
        resource_type="agent",
        resource_id=agent.id,
        action="delete",
    )
    session.add(event)
    await session.commit()

    await session.delete(agent)
    await session.flush()

    assert await session.get(Agent, agent.id) is None
    await session.refresh(event)
    assert event.resource_id is not None


async def test_audit_events_are_scoped_to_one_tenant(session: AsyncSession) -> None:
    first = await make_organization(session)
    second = await make_organization(session)
    session.add_all(
        [
            AuditEvent(organization_id=first.id, event_type="a", action="create"),
            AuditEvent(organization_id=second.id, event_type="b", action="create"),
        ]
    )
    await session.flush()

    rows = (
        (await session.execute(select(AuditEvent).where(AuditEvent.organization_id == first.id)))
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert rows[0].event_type == "a"
