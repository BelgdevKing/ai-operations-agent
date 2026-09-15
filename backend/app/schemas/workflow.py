"""Request and response schemas for workflows.

What a client may say, and what it is told back.

**A definition is data.** The create request carries a JSON document that the
domain model parses and refuses if it is not exactly one of the four step shapes
- there is no field in it that can name a module, a callable or an expression,
and an unknown ``type`` or an unknown condition operator is a 422 rather than
something the engine tries to interpret.

**Input is untrusted.** It is bounded here and bounded again in the service,
which is the copy that protects any future caller not coming through this
schema.

**Step output is not published.** A run's final ``output`` is - that is what the
caller asked for - but each individual step's payload is the workflow's working,
and a list of steps exists so somebody can see *what happened*, not so an
interface can show every record a tool read along the way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import RunStatus, StepRunStatus, WorkflowStatus, WorkflowStepType
from app.models.workflow import Workflow, WorkflowStepRun
from app.services.workflow_execution import WorkflowRunView

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1_000

# Matches the agent run endpoint's, for the same reason: an opaque client token,
# bounded and restricted to printable ASCII so it fits its column and cannot
# smuggle control characters into a log line.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:\-]{1,255}$"


class WorkflowCreateRequest(BaseModel):
    """Add a new draft version of a workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        description="The procedure's name. A second version of an existing name "
        "becomes the next version of it rather than a separate workflow.",
    )
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    definition: dict[str, Any] = Field(
        description="The steps and the transitions between them. Validated "
        "against the workflow document schema; anything that is not one of the "
        "known step types is refused."
    )


class WorkflowRunRequest(BaseModel):
    """Start a run of an active workflow version."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(
        default_factory=dict,
        description="What the workflow starts with, readable as `$.input.…`. "
        "Cannot name organization, user, run or request identity - those are "
        "established by the server from a verified membership, and a payload "
        "that tries is refused rather than ignored.",
    )


class WorkflowSummary(BaseModel):
    """One version of a workflow, without its document."""

    id: uuid.UUID
    name: str
    description: str | None
    version: int
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Workflow) -> WorkflowSummary:
        return cls(
            id=record.id,
            name=record.name,
            description=record.description,
            version=record.version,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class WorkflowDetail(WorkflowSummary):
    """One version, with the document it will run.

    The definition is the organization's own, written by somebody there, so
    returning it discloses nothing they did not put in.
    """

    definition: dict[str, Any]

    @classmethod
    def from_record(cls, record: Workflow) -> WorkflowDetail:
        return cls(
            id=record.id,
            name=record.name,
            description=record.description,
            version=record.version,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            definition=record.definition,
        )


class WorkflowStepRunResponse(BaseModel):
    """One step of a run, as much of it as a client may see.

    Which step, of what kind, how it ended and when. Not what it produced: the
    payload is the workflow's working, and publishing every record a tool read
    so that an interface can show its progress is exactly the trade this
    codebase does not make.
    """

    id: uuid.UUID
    step_key: str
    step_type: WorkflowStepType
    position: int
    status: StepRunStatus
    error_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_record(cls, record: WorkflowStepRun) -> WorkflowStepRunResponse:
        return cls(
            id=record.id,
            step_key=record.step_key,
            step_type=record.step_type,
            position=record.position,
            status=record.status,
            error_code=record.error_code,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )


class WorkflowPendingApproval(BaseModel):
    """What a paused workflow is waiting for.

    Enough for a person to understand the decision: which step, which kind of
    action, and why it is gated. **Not** what the tool was asked to do - the
    arguments are business data and the backend does not publish them.
    """

    id: uuid.UUID
    step_key: str | None
    tool_name: str | None
    action: str
    reason: str | None
    requested_at: datetime
    requested_by: uuid.UUID


class WorkflowRunResponse(BaseModel):
    """The state of one run, read from the durable record."""

    run_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version: int
    status: RunStatus

    current_step: str | None = None
    step_count: int
    steps: list[WorkflowStepRunResponse] = Field(default_factory=list)

    output: dict[str, Any] | None = Field(
        default=None,
        description="What the run produced, from its last step. Present once it has succeeded.",
    )
    error_code: str | None = Field(
        default=None,
        description="Stable code when the run stopped, for example "
        "workflow_step_failed or workflow_run_abandoned.",
    )
    approval: WorkflowPendingApproval | None = Field(
        default=None, description="Set while the run is awaiting approval, and only then."
    )

    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_view(cls, view: WorkflowRunView) -> WorkflowRunResponse:
        record = view.record
        approval = view.pending_approval

        step_by_id = {step.id: step for step in view.steps}
        pending_step = (
            step_by_id.get(approval.workflow_step_run_id)
            if approval is not None and approval.workflow_step_run_id is not None
            else None
        )

        return cls(
            run_id=record.id,
            workflow_id=record.workflow_id,
            workflow_version=record.workflow_version,
            status=record.status,
            current_step=record.current_step,
            step_count=record.step_count,
            steps=[WorkflowStepRunResponse.from_record(step) for step in view.steps],
            output=record.output_data,
            error_code=record.error_code,
            approval=(
                WorkflowPendingApproval(
                    id=approval.id,
                    step_key=pending_step.step_key if pending_step else None,
                    tool_name=approval.tool_name,
                    action=approval.action,
                    reason=approval.reason,
                    requested_at=approval.requested_at,
                    requested_by=approval.requested_by,
                )
                if approval is not None
                else None
            ),
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )
