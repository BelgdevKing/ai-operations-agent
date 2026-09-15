"""Recording that something happened.

A handful of functions, not a subsystem. The `audit_events` table has existed
since the schema phase; these are the writers, and they are deliberately the
only ones.

**Written at the service layer, deliberately.** The agent runtime has no database
session and must not acquire one - it is the part that talks to a model and a
tool framework, and giving it a session would make every future runtime change a
question about transactions. The layer that orchestrates a run already owns a
request-scoped session, so it is the right place to record what the request did.

What goes in is identifiers, counts, codes and an outcome. What stays out, in
every function below:

* the prompt, the answer, and any part of the conversation;
* tool arguments and tool results;
* credentials, tokens, authorization headers, provider responses;
* exception text - only the stable ``error_code`` a class defines.

A tool's *name* is included. It says which kind of record a run touched, which
is most of the value of an audit trail; what it was asked and what it returned is
the tenant's business data and lives in the conversation instead.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun
from app.models.agent_run import AgentRunRecord
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.workflow import WorkflowRun

logger = logging.getLogger(__name__)

AGENT_RUN_EVENT = "agent.run"
AGENT_RUN_CREATED_EVENT = "agent.run.created"
AGENT_RUN_RESOURCE = "agent_run"

WORKFLOW_RUN_EVENT = "workflow.run"
WORKFLOW_RUN_CREATED_EVENT = "workflow.run.created"
WORKFLOW_RUN_RESOURCE = "workflow_run"

APPROVAL_REQUESTED_EVENT = "approval.requested"
APPROVAL_APPROVED_EVENT = "approval.approved"
APPROVAL_REJECTED_EVENT = "approval.rejected"
APPROVAL_RESOURCE = "approval"


async def record_agent_run(session: AsyncSession, run: AgentRun) -> AuditEvent:
    """Add a sanitized record of where one agent run ended up.

    Written whenever a request stops advancing a run - because it finished,
    failed, was cancelled, or paused for an approval. A run therefore leaves one
    creation event and one of these per request that drove it, which is what
    makes a resumed run readable as a single story.

    Flushed but not committed: the request's own transaction decides whether it
    survives, so an audit row cannot outlive a request that failed afterwards.
    Flushed rather than merely added because the session does not autoflush, and
    a row that only appears at commit time is a row no test can see.
    """
    return await _add(
        session,
        organization_id=run.organization_id,
        user_id=run.user_id,
        event_type=AGENT_RUN_EVENT,
        resource_type=AGENT_RUN_RESOURCE,
        resource_id=run.id,
        action=run.status.value,
        metadata={
            "request_id": run.request_id,
            "agent_id": str(run.agent_id),
            "step_count": run.step_count,
            "tool_calls": [call.tool_name for call in run.tool_calls],
            "total_tokens": run.usage.total_tokens,
            "error_code": run.error_code,
        },
    )


async def record_agent_run_created(
    session: AsyncSession, record: AgentRunRecord, *, idempotent_replay: bool = False
) -> AuditEvent:
    """Add a record that a durable run was created.

    ``idempotency_key`` is **not** written. It is a client-chosen token that may
    encode anything the client likes, and an audit trail is not the place to
    keep somebody else's opaque string; that a request carried one is recorded
    instead.
    """
    return await _add(
        session,
        organization_id=record.organization_id,
        user_id=record.user_id,
        event_type=AGENT_RUN_CREATED_EVENT,
        resource_type=AGENT_RUN_RESOURCE,
        resource_id=record.id,
        action=record.status.value,
        metadata={
            "request_id": record.request_id,
            "agent_id": str(record.agent_id),
            "idempotent": record.idempotency_key is not None,
            "replayed": idempotent_replay,
        },
    )


async def record_workflow_run_created(
    session: AsyncSession, record: WorkflowRun, *, idempotent_replay: bool = False
) -> AuditEvent:
    """Add a record that a durable workflow run was created.

    The *version* is recorded as well as the workflow id, because which edition
    of a business process ran is exactly the question an audit trail is asked
    months later. The input is not: it is the caller's data, and an audit row is
    read by more people than any other table in the system.
    """
    return await _add(
        session,
        organization_id=record.organization_id,
        user_id=record.user_id,
        event_type=WORKFLOW_RUN_CREATED_EVENT,
        resource_type=WORKFLOW_RUN_RESOURCE,
        resource_id=record.id,
        action=record.status.value,
        metadata={
            "request_id": record.request_id,
            "workflow_id": str(record.workflow_id),
            "workflow_version": record.workflow_version,
            "idempotent": record.idempotency_key is not None,
            "replayed": idempotent_replay,
        },
    )


async def record_workflow_run_outcome(session: AsyncSession, record: WorkflowRun) -> AuditEvent:
    """Add a record of where a workflow run got to.

    Written whenever a request stops advancing a run - because it finished,
    failed, or paused for somebody. A run therefore leaves one creation event
    and one of these per request that drove it, which is what makes a workflow
    resumed after an approval read as a single story.
    """
    return await _add(
        session,
        organization_id=record.organization_id,
        user_id=record.user_id,
        event_type=WORKFLOW_RUN_EVENT,
        resource_type=WORKFLOW_RUN_RESOURCE,
        resource_id=record.id,
        action=record.status.value,
        metadata={
            "request_id": record.request_id,
            "workflow_id": str(record.workflow_id),
            "workflow_version": record.workflow_version,
            "step_count": record.step_count,
            "current_step": record.current_step,
            "error_code": record.error_code,
        },
    )


async def record_approval_requested(session: AsyncSession, approval: Approval) -> AuditEvent:
    """Add a record that a person was asked about an action."""
    return await _add(
        session,
        organization_id=approval.organization_id,
        user_id=approval.requested_by,
        event_type=APPROVAL_REQUESTED_EVENT,
        resource_type=APPROVAL_RESOURCE,
        resource_id=approval.id,
        action=approval.status.value,
        metadata={
            "run_id": str(approval.run_id) if approval.run_id else None,
            "workflow_run_id": (
                str(approval.workflow_run_id) if approval.workflow_run_id else None
            ),
            "tool_execution_id": (
                str(approval.tool_execution_id) if approval.tool_execution_id else None
            ),
            "tool_name": approval.tool_name,
        },
    )


async def record_approval_decision(
    session: AsyncSession, approval: Approval, *, decided_by: uuid.UUID
) -> AuditEvent:
    """Add a record of who decided, and which way.

    The decision's *reason* is not recorded, because nobody is asked for one -
    and if they were, it would be free text a person wrote about a specific
    business action, which belongs with the approval rather than in a trail
    designed to be readable by anyone who can see the organization's history.
    """
    approved = approval.status.value == "approved"
    event_type = APPROVAL_APPROVED_EVENT if approved else APPROVAL_REJECTED_EVENT

    return await _add(
        session,
        organization_id=approval.organization_id,
        user_id=decided_by,
        event_type=event_type,
        resource_type=APPROVAL_RESOURCE,
        resource_id=approval.id,
        action=approval.status.value,
        metadata={
            "run_id": str(approval.run_id) if approval.run_id else None,
            "workflow_run_id": (
                str(approval.workflow_run_id) if approval.workflow_run_id else None
            ),
            "tool_execution_id": (
                str(approval.tool_execution_id) if approval.tool_execution_id else None
            ),
            "tool_name": approval.tool_name,
            "requested_by": str(approval.requested_by),
        },
    )


async def _add(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID,
    action: str,
    metadata: dict[str, Any],
) -> AuditEvent:
    event = AuditEvent(
        organization_id=organization_id,
        user_id=user_id,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        event_metadata=metadata,
    )
    session.add(event)
    await session.flush()

    logger.info(
        "Audit event recorded",
        extra={
            "context": {
                "event_type": event_type,
                "resource_type": resource_type,
                "resource_id": str(resource_id),
                "organization_id": str(organization_id),
                "action": action,
            }
        },
    )
    return event
