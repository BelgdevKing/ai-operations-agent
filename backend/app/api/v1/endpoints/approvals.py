"""The human approval queue.

    GET  /approvals               any active member may read the queue
    GET  /approvals/{id}          any active member may read one
    POST /approvals/{id}/approve  admin or owner only
    POST /approvals/{id}/reject   admin or owner only

**The role dependency is the security boundary.** Whether an interface offers a
button decides nothing: a member who posts to the approve path with a correct
token and a correct organization is refused here, by the same
``require_role(ADMIN)`` that guards member administration, checked against the
database on every request. The frontend's copy of the rule exists only so a
member is not shown a control that would fail.

Admin rather than a new permission, deliberately. The authorization model has
three roles and no per-action permissions, and inventing one for this would mean
building a permission system to hold a single value. Approving a destructive
action is administrative in exactly the way changing somebody's role is, and
owners qualify by rank - so the smallest explicit permission that expresses
"only an administrator decides this" is the admin role.

Approve and reject are separate routes rather than one route with a body, so
which decision was made is in the request line: it appears in access logs, it
cannot be got wrong by a client sending the wrong field, and a proxy replaying a
request cannot turn a rejection into an approval.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import ApprovalServiceDep, RequireAdmin, RequireMember
from app.schemas.approval import ApprovalDecisionResponse, ApprovalResponse
from app.schemas.common import ErrorResponse

router = APIRouter()

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {
        "model": ErrorResponse,
        "description": "Not a member, or not an administrator of this organization",
    },
    404: {"model": ErrorResponse, "description": "No such approval for this organization"},
    409: {
        "model": ErrorResponse,
        "description": "Already decided, or the run can no longer continue",
    },
}

DECISION_RESPONSES: dict[int | str, dict[str, object]] = {
    **RESPONSES,
    413: {"model": ErrorResponse, "description": "The conversation grew too large to continue"},
    429: {"model": ErrorResponse, "description": "The provider is rate limiting; retry shortly"},
    502: {"model": ErrorResponse, "description": "The provider failed while resuming the run"},
    504: {"model": ErrorResponse, "description": "The provider did not respond in time"},
}


@router.get(
    "",
    response_model=list[ApprovalResponse],
    summary="Actions waiting on a person",
    responses={401: RESPONSES[401], 403: RESPONSES[403]},
)
async def list_approvals(
    membership: RequireMember,
    approvals: ApprovalServiceDep,
    limit: int = 50,
) -> list[ApprovalResponse]:
    """This organization's pending approvals, oldest first.

    Readable by any active member. Seeing that the agent wants to cancel a
    shipment is not the same as being able to let it, and a queue only one
    person can see is a queue nobody chases.
    """
    del membership
    pending = await approvals.list_pending(limit=max(1, min(limit, 100)))
    return [ApprovalResponse.from_record(record) for record in pending]


@router.get(
    "/{approval_id}",
    response_model=ApprovalResponse,
    summary="One approval request",
    responses=RESPONSES,
)
async def get_approval(
    approval_id: uuid.UUID,
    membership: RequireMember,
    approvals: ApprovalServiceDep,
) -> ApprovalResponse:
    """Read one of this organization's approvals, decided or not."""
    del membership
    return ApprovalResponse.from_record(await approvals.get(approval_id))


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
    summary="Approve the action and resume whatever was waiting",
    responses=DECISION_RESPONSES,
)
async def approve(
    approval_id: uuid.UUID,
    membership: RequireAdmin,
    approvals: ApprovalServiceDep,
) -> ApprovalDecisionResponse:
    """Let the action happen, and carry the paused process forward.

    The tool runs **at most once** for this approval's execution, whatever
    happens above: the decision is taken by a conditional update that only one
    request can win, and the execution is claimed by a second one. A repeated
    approval is a 409, not a second cancellation.

    The response says what was resumed - an agent run, a workflow run, or both
    where an agent step inside a workflow paused - as it stands afterwards.
    """
    del membership
    return ApprovalDecisionResponse.from_decision(await approvals.decide(approval_id, approve=True))


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDecisionResponse,
    summary="Decline the action and let the process respond",
    responses=DECISION_RESPONSES,
)
async def reject(
    approval_id: uuid.UUID,
    membership: RequireAdmin,
    approvals: ApprovalServiceDep,
) -> ApprovalDecisionResponse:
    """Decline the action, and let whatever was waiting say so.

    The tool is never executed. For an agent, the refusal goes back into the
    conversation as an ordinary tool outcome and its own bounded loop decides
    what to tell the user. For a workflow, it takes the path the definition
    declared for a refusal, or stops with ``workflow_approval_rejected`` if it
    declared none. Neither is turned into a failure of the platform, and neither
    is reported as if the action had been performed.
    """
    del membership
    return ApprovalDecisionResponse.from_decision(
        await approvals.decide(approval_id, approve=False)
    )
