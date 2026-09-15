"""Request and response schemas for human approvals.

What an approver is shown, and what they are not.

**Shown:** which tool, why it is gated, which run it belongs to, who asked,
when, and - once decided - who decided and which way. That is enough to make an
informed decision about *a kind of action*, and to hold somebody accountable for
having made it.

**Not shown:** the arguments. They are the tenant's business data, they live in
the conversation where the rest of the exchange does, and an approval queue is
not the place to publish them to anyone who can read a list. ``parameters`` is
not exposed at all - agent approvals leave it empty, and a workflow approval's
contents are not this endpoint's to hand out.

There is no request body for a decision. Approving is a POST to the approve
path and rejecting is a POST to the reject path, so which decision was made is
the route rather than a field a client could get wrong.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.schemas.agent import AgentRunResponse
from app.schemas.workflow import WorkflowRunResponse
from app.services.approvals import ApprovalDecision


class ApprovalResponse(BaseModel):
    """One approval request."""

    id: uuid.UUID
    status: ApprovalStatus

    run_id: uuid.UUID | None = Field(
        default=None, description="The agent run waiting on this decision."
    )
    conversation_id: uuid.UUID | None = None
    tool_execution_id: uuid.UUID | None = Field(
        default=None,
        description="The execution this decision authorises. The tool runs at "
        "most once under this identity, however many times it is approved.",
    )

    tool_name: str | None = None
    action: str = Field(description="What is being asked for, by name.")
    reason: str | None = Field(
        default=None, description="Why a person has to agree. Never the arguments."
    )

    requested_by: uuid.UUID
    requested_at: datetime

    decided_by: uuid.UUID | None = Field(default=None, description="Who approved or rejected it.")
    decided_at: datetime | None = None

    @classmethod
    def from_record(cls, approval: Approval) -> ApprovalResponse:
        """Project one row onto the public contract.

        ``approved_by`` and ``approved_at`` are published as ``decided_by`` and
        ``decided_at``: the columns predate rejection being representable, and
        a field called ``approved_by`` on a rejected approval reads as a bug
        every time somebody sees it.
        """
        return cls(
            id=approval.id,
            status=approval.status,
            run_id=approval.run_id,
            conversation_id=approval.conversation_id,
            tool_execution_id=approval.tool_execution_id,
            tool_name=approval.tool_name,
            action=approval.action,
            reason=approval.reason,
            requested_by=approval.requested_by,
            requested_at=approval.requested_at,
            decided_by=approval.approved_by,
            decided_at=approval.approved_at,
        )


class ApprovalDecisionResponse(BaseModel):
    """What a decision did.

    Two processes can pause on an approval, and the response says which one was
    carried forward rather than leaving a client to infer it from the shape of
    the payload. Both are present for an agent step inside a workflow: the agent
    run resumed, and then the workflow that was waiting on it.
    """

    approval: ApprovalResponse
    agent_run: AgentRunResponse | None = Field(
        default=None, description="The agent run this decision resumed, if it resumed one."
    )
    workflow_run: WorkflowRunResponse | None = Field(
        default=None, description="The workflow run this decision resumed, if it resumed one."
    )

    @classmethod
    def from_decision(cls, decision: ApprovalDecision) -> ApprovalDecisionResponse:
        return cls(
            approval=ApprovalResponse.from_record(decision.approval),
            agent_run=(
                AgentRunResponse.from_view(decision.agent_run)
                if decision.agent_run is not None
                else None
            ),
            workflow_run=(
                WorkflowRunResponse.from_view(decision.workflow_run)
                if decision.workflow_run is not None
                else None
            ),
        )
