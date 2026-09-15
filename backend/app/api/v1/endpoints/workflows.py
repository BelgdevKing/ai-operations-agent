"""Managing workflows and running them.

    POST /ai/workflows                      draft a version   (admin)
    GET  /ai/workflows                      list versions     (member)
    GET  /ai/workflows/{id}                 one version       (member)
    POST /ai/workflows/{id}/activate        make it runnable  (admin)
    POST /ai/workflows/{id}/deactivate      retire it         (admin)
    POST /ai/workflows/{id}/runs            start a run       (member)
    GET  /ai/workflows/{id}/runs/{run_id}   one run           (member)
    GET  /ai/workflows/{id}/runs/{run_id}/steps               (member)

The smallest surface that exercises the engine. Writing a definition is an
administrative act - it decides which tools this organization's automation may
call and on whose behalf - so drafting and activating need the admin role, the
same one that decides approvals and changes memberships. Starting a run is
ordinary work, so any active member may do it.

As thin as every other endpoint module here: authenticate, establish the tenant,
convert the request, hand off. It does not validate a graph, run a step or pick
a provider.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.deps import RequireAdmin, RequireMember, WorkflowServiceDep
from app.core.context import get_request_id
from app.schemas.common import ErrorResponse
from app.schemas.workflow import (
    IDEMPOTENCY_KEY_PATTERN,
    WorkflowCreateRequest,
    WorkflowDetail,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowStepRunResponse,
    WorkflowSummary,
)

router = APIRouter()

IDEMPOTENCY_HEADER = "Idempotency-Key"

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {
        "model": ErrorResponse,
        "description": "Not a member, or not an administrator of this organization",
    },
    404: {"model": ErrorResponse, "description": "No such workflow or run for this organization"},
    409: {"model": ErrorResponse, "description": "That workflow version is not active"},
    413: {"model": ErrorResponse, "description": "A step produced more than a run may carry"},
    422: {
        "model": ErrorResponse,
        "description": "The definition or the input could not be accepted",
    },
    502: {"model": ErrorResponse, "description": "A step's provider or tool failed"},
}

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_HEADER,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "Opaque token making this run repeatable. Sending the same key again "
            "for the same organization returns the run it already created rather "
            "than starting a second one - which is what makes a retry safe when a "
            "workflow may have already cancelled a shipment."
        ),
    ),
]


# -- Definitions --------------------------------------------------------------


@router.post(
    "",
    response_model=WorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a workflow version",
    responses=RESPONSES,
)
async def create_workflow(
    payload: WorkflowCreateRequest,
    membership: RequireAdmin,
    workflows: WorkflowServiceDep,
) -> WorkflowDetail:
    """Store a new draft.

    A draft is never runnable. Validating it against the tools and agents that
    actually exist happens when somebody activates it, which is the deliberate
    act that says "this may now run against our business records".
    """
    del membership
    record = await workflows.create(
        name=payload.name, description=payload.description, definition=payload.definition
    )
    return WorkflowDetail.from_record(record)


@router.get(
    "",
    response_model=list[WorkflowSummary],
    summary="Workflow versions this organization has",
    responses={401: RESPONSES[401], 403: RESPONSES[403]},
)
async def list_workflows(
    membership: RequireMember,
    workflows: WorkflowServiceDep,
    limit: int = 50,
) -> list[WorkflowSummary]:
    """Every version, newest first within each name."""
    del membership
    records = await workflows.list_definitions(limit=max(1, min(limit, 100)))
    return [WorkflowSummary.from_record(record) for record in records]


@router.get(
    "/{workflow_id}",
    response_model=WorkflowDetail,
    summary="One workflow version, with its definition",
    responses=RESPONSES,
)
async def get_workflow(
    workflow_id: uuid.UUID,
    membership: RequireMember,
    workflows: WorkflowServiceDep,
) -> WorkflowDetail:
    """Read one of this organization's workflow versions.

    A workflow belonging to another tenant answers 404, exactly as one that does
    not exist - so an id cannot be used to discover another organization's
    processes.
    """
    del membership
    return WorkflowDetail.from_record(await workflows.get_definition(workflow_id))


@router.post(
    "/{workflow_id}/activate",
    response_model=WorkflowDetail,
    summary="Validate a version and make it runnable",
    responses=RESPONSES,
)
async def activate_workflow(
    workflow_id: uuid.UUID,
    membership: RequireAdmin,
    workflows: WorkflowServiceDep,
) -> WorkflowDetail:
    """Check the definition against the world, then make it the current edition.

    A definition that names a tool which is not installed, an agent this
    organization cannot use, or a graph with no ending is refused with the list
    of problems - and stays a draft, so nothing can start it.

    Activating a version retires the previous one. Runs already under way keep
    the edition they started with.
    """
    del membership
    return WorkflowDetail.from_record(await workflows.activate(workflow_id))


@router.post(
    "/{workflow_id}/deactivate",
    response_model=WorkflowDetail,
    summary="Stop a version being startable",
    responses=RESPONSES,
)
async def deactivate_workflow(
    workflow_id: uuid.UUID,
    membership: RequireAdmin,
    workflows: WorkflowServiceDep,
) -> WorkflowDetail:
    """Retire a version. Runs already under way are unaffected."""
    del membership
    return WorkflowDetail.from_record(await workflows.deactivate(workflow_id))


# -- Runs ---------------------------------------------------------------------


@router.post(
    "/{workflow_id}/runs",
    response_model=WorkflowRunResponse,
    summary="Start a workflow run",
    responses=RESPONSES,
)
async def start_run(
    workflow_id: uuid.UUID,
    payload: WorkflowRunRequest,
    membership: RequireMember,
    workflows: WorkflowServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> WorkflowRunResponse:
    """Run an active workflow version, durably.

    The organization is the caller's verified membership - there is no field in
    the request that could name another, and a workflow belonging to a different
    tenant is reported as not found.

    **The run may not finish.** If a step needs a person, the run is left
    ``awaiting_approval`` with the pending decision described in ``approval``,
    and continues - as the same run - once somebody decides. A step that fails
    leaves the run ``failed`` with a code; that is an answer about the business
    process, not an HTTP error, so it comes back as a 200 and a run to read.
    """
    del membership
    view = await workflows.start(
        workflow_id,
        input_data=payload.input,
        idempotency_key=idempotency_key,
        request_id=get_request_id(),
    )
    return WorkflowRunResponse.from_view(view)


@router.get(
    "/{workflow_id}/runs/{run_id}",
    response_model=WorkflowRunResponse,
    summary="One workflow run",
    responses=RESPONSES,
)
async def get_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    membership: RequireMember,
    workflows: WorkflowServiceDep,
) -> WorkflowRunResponse:
    """Read one of this organization's runs.

    The durable record, which is what makes a run survive the request that
    started it: an interface closed while an approval was pending comes back and
    finds the run exactly where it was left.
    """
    del membership, workflow_id
    return WorkflowRunResponse.from_view(await workflows.get_run(run_id))


@router.get(
    "/{workflow_id}/runs/{run_id}/steps",
    response_model=list[WorkflowStepRunResponse],
    summary="What each step of a run did",
    responses=RESPONSES,
)
async def list_run_steps(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    membership: RequireMember,
    workflows: WorkflowServiceDep,
) -> list[WorkflowStepRunResponse]:
    """The steps of one run, in the order they executed.

    Which step, of what kind, how it ended and when. Not what each produced -
    see :class:`WorkflowStepRunResponse`.
    """
    del membership, workflow_id
    steps = await workflows.steps_of(run_id)
    return [WorkflowStepRunResponse.from_record(step) for step in steps]
