"""Builders for test rows.

Each takes a session, inserts, flushes so the row gets its identity, and
returns it. Names and slugs are made unique per call so tests never collide.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Agent,
    Conversation,
    Organization,
    OrganizationMember,
    Tool,
    User,
    Workflow,
    WorkflowRun,
    WorkflowStep,
)
from app.models.enums import MemberRole, WorkflowStepType


def _suffix() -> str:
    return uuid.uuid4().hex[:12]


async def make_organization(session: AsyncSession, **kwargs: Any) -> Organization:
    unique = _suffix()
    organization = Organization(
        name=kwargs.pop("name", f"Org {unique}"),
        slug=kwargs.pop("slug", f"org-{unique}"),
        **kwargs,
    )
    session.add(organization)
    await session.flush()
    return organization


async def make_user(session: AsyncSession, **kwargs: Any) -> User:
    user = User(email=kwargs.pop("email", f"user-{_suffix()}@example.test"), **kwargs)
    session.add(user)
    await session.flush()
    return user


async def make_member(
    session: AsyncSession,
    organization: Organization,
    user: User,
    role: MemberRole = MemberRole.MEMBER,
    **kwargs: Any,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=organization.id,
        user_id=user.id,
        role=role,
        **kwargs,
    )
    session.add(member)
    await session.flush()
    return member


async def make_agent(session: AsyncSession, organization: Organization, **kwargs: Any) -> Agent:
    agent = Agent(
        organization_id=organization.id,
        name=kwargs.pop("name", f"Agent {_suffix()}"),
        model=kwargs.pop("model", "claude-sonnet-5"),
        **kwargs,
    )
    session.add(agent)
    await session.flush()
    return agent


async def make_tool(session: AsyncSession, **kwargs: Any) -> Tool:
    tool = Tool(name=kwargs.pop("name", f"tool_{_suffix()}"), **kwargs)
    session.add(tool)
    await session.flush()
    return tool


async def make_conversation(
    session: AsyncSession,
    organization: Organization,
    user: User,
    agent: Agent,
    **kwargs: Any,
) -> Conversation:
    conversation = Conversation(
        organization_id=organization.id,
        user_id=user.id,
        agent_id=agent.id,
        **kwargs,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def make_workflow(
    session: AsyncSession, organization: Organization, **kwargs: Any
) -> Workflow:
    workflow = Workflow(
        organization_id=organization.id,
        name=kwargs.pop("name", f"Workflow {_suffix()}"),
        **kwargs,
    )
    session.add(workflow)
    await session.flush()
    return workflow


async def make_step(
    session: AsyncSession,
    workflow: Workflow,
    step_order: int,
    step_type: WorkflowStepType = WorkflowStepType.TOOL_CALL,
    **kwargs: Any,
) -> WorkflowStep:
    step = WorkflowStep(
        workflow_id=workflow.id,
        name=kwargs.pop("name", f"Step {step_order}"),
        step_type=step_type,
        step_order=step_order,
        **kwargs,
    )
    session.add(step)
    await session.flush()
    return step


async def make_run(
    session: AsyncSession, workflow: Workflow, organization: Organization, **kwargs: Any
) -> WorkflowRun:
    run = WorkflowRun(
        workflow_id=workflow.id,
        organization_id=organization.id,
        **kwargs,
    )
    session.add(run)
    await session.flush()
    return run
