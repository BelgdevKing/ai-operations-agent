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

**Shown, since the human-in-the-loop phase:** a one-line summary of the action
and the labelled fields behind it. Those are not the arguments with some parts
removed - they are what the *tool declared*, in its own code, that an approver
may see, projected once when the approval was requested. A tool that declares
nothing produces an approval with no summary, and the response says so by
omitting it rather than by falling back to anything.

Which decision was made is the route: approving is a POST to the approve path
and rejecting a POST to the reject path. The body carries at most a reason the
person typed. It carries nothing about the action - not the tool, not the
execution, not the run, not the organization - because all of those are read
from the persisted approval. There is therefore no request a client can send
that alters what it is approving, and that is a property of the schema rather
than a check somewhere downstream.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.approval import Approval
from app.models.enums import ApprovalStatus
from app.schemas.agent import AgentRunResponse
from app.schemas.common import ApprovalField
from app.schemas.workflow import WorkflowRunResponse
from app.services.approvals import MAX_DECISION_REASON_CHARACTERS, ApprovalDecision

APPROVED_RUNS_TOOL = (
    "{tool} runs once, as the exact execution this approval names, and the "
    "paused run continues from where it stopped."
)
APPROVED_CONTINUES = "The paused run continues along the path its definition declares for a yes."

REJECTED_SKIPS_TOOL = (
    "{tool} is never run. The paused run continues and reports the refusal - "
    "a refusal is an outcome of the process, not a failure of the platform."
)
REJECTED_STOPS = (
    "The paused run takes the path its definition declares for a refusal, or "
    "stops if it declares none. Nothing is performed either way."
)


class ApprovalResponse(BaseModel):
    """One approval request, as an authorized reviewer may see it."""

    id: uuid.UUID
    organization_id: uuid.UUID = Field(
        description="Which tenant this belongs to. Always the caller's own - "
        "the queue is scoped before it is read - and published so a client "
        "holding responses from either side of a tenant switch can tell them "
        "apart."
    )
    status: ApprovalStatus

    run_id: uuid.UUID | None = Field(
        default=None, description="The agent run waiting on this decision."
    )
    conversation_id: uuid.UUID | None = None
    workflow_run_id: uuid.UUID | None = Field(
        default=None, description="The workflow run waiting on this decision."
    )
    workflow_step_run_id: uuid.UUID | None = Field(
        default=None, description="The exact step of it that is paused."
    )
    tool_execution_id: uuid.UUID | None = Field(
        default=None,
        description="The execution this decision authorises. The tool runs at "
        "most once under this identity, however many times it is approved.",
    )

    tool_name: str | None = None
    action: str = Field(description="What is being asked for, by name.")
    summary: str | None = Field(
        default=None,
        description="What is being proposed, in one line: 'Cancel shipment "
        "ABC123'. Built from the tool's own declaration when the approval was "
        "requested. Absent where the tool declared none.",
    )
    summary_fields: list[ApprovalField] = Field(
        default_factory=list,
        description="The labelled values behind the summary. An allow-list the "
        "tool declared, never the argument payload.",
    )
    reason: str | None = Field(
        default=None,
        description="Why a person has to agree - the framework's reason, from "
        "the tool's safety classification or the workflow's own definition. "
        "Never a sentence the model wrote about its own authority, and never "
        "the arguments.",
    )

    effect_if_approved: str = Field(description="What happens on a yes.")
    effect_if_rejected: str = Field(description="What happens on a no.")

    requested_by: uuid.UUID
    requested_at: datetime
    expires_at: datetime | None = Field(
        default=None,
        description="When this stops being decidable. Afterwards the action "
        "never runs and the paused run is stopped with approval_expired.",
    )

    decided_by: uuid.UUID | None = Field(default=None, description="Who approved or rejected it.")
    decided_at: datetime | None = None
    decision_reason: str | None = Field(
        default=None, description="What the deciding person wrote, if anything."
    )

    @classmethod
    def from_record(cls, approval: Approval) -> ApprovalResponse:
        """Project one row onto the public contract.

        ``approved_by`` and ``approved_at`` are published as ``decided_by`` and
        ``decided_at``: the columns predate rejection being representable, and
        a field called ``approved_by`` on a rejected approval reads as a bug
        every time somebody sees it.

        ``parameters`` is not published, and there is no field here that could
        publish it.
        """
        gates_execution = approval.tool_execution_id is not None

        return cls(
            id=approval.id,
            organization_id=approval.organization_id,
            status=approval.status,
            run_id=approval.run_id,
            conversation_id=approval.conversation_id,
            workflow_run_id=approval.workflow_run_id,
            workflow_step_run_id=approval.workflow_step_run_id,
            tool_execution_id=approval.tool_execution_id,
            tool_name=approval.tool_name,
            action=approval.action,
            summary=approval.summary,
            summary_fields=[ApprovalField(**field) for field in approval.summary_fields],
            reason=approval.reason,
            effect_if_approved=(
                APPROVED_RUNS_TOOL.format(tool=approval.tool_name)
                if gates_execution
                else APPROVED_CONTINUES
            ),
            effect_if_rejected=(
                REJECTED_SKIPS_TOOL.format(tool=approval.tool_name)
                if gates_execution
                else REJECTED_STOPS
            ),
            requested_by=approval.requested_by,
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
            decided_by=approval.approved_by,
            decided_at=approval.approved_at,
            decision_reason=approval.decision_reason,
        )


class ApprovalQueue(BaseModel):
    """One page of the queue.

    An object rather than a bare list, so the cursor has somewhere to live.
    """

    approvals: list[ApprovalResponse]
    next_cursor: uuid.UUID | None = Field(
        default=None,
        description="Pass back as ?cursor= to read the next page. Null on the "
        "last page. A full page always carries one, because whether anything "
        "follows it cannot be known without asking - and asking is the next "
        "request.",
    )


class ApprovalDecisionRequest(BaseModel):
    """What a person may send with a decision: a sentence, and nothing else.

    ``extra="forbid"`` is the security property here, not a tidiness preference.
    A client that tries to send ``tool_name``, ``tool_execution_id``, ``run_id``,
    ``organization_id`` or an ``arguments`` payload is refused with a 422 rather
    than having the field quietly ignored - so "fetch an approval, change what
    it does, submit it" fails loudly at the edge, instead of relying on every
    handler behind it remembering not to read the field.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=MAX_DECISION_REASON_CHARACTERS,
        description="Why the decision went this way, in the deciding person's "
        "own words. Optional, bounded, stored as plain text and rendered as "
        "text. Not a place for anything secret - everybody who can read this "
        "organization's queue can read it.",
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
