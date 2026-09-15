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

**The decision body cannot change the decision's subject.** It has one optional
field, a reason, and ``extra="forbid"`` on the schema - so a request carrying a
tool name, an execution id, a run id or an arguments payload is refused at the
edge with a 422 rather than being quietly ignored. Everything about the action
comes from the persisted approval. There is no sequence of requests that fetches
an approval, alters what it proposed, and submits it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ApprovalServiceDep, RequireAdmin, RequireMember
from app.models.enums import ApprovalStatus
from app.schemas.approval import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalQueue,
    ApprovalResponse,
)
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
    410: {"model": ErrorResponse, "description": "The approval expired before it was decided"},
}

DEFAULT_PAGE = 50
MAX_PAGE = 100

DECISION_RESPONSES: dict[int | str, dict[str, object]] = {
    **RESPONSES,
    413: {"model": ErrorResponse, "description": "The conversation grew too large to continue"},
    429: {"model": ErrorResponse, "description": "The provider is rate limiting; retry shortly"},
    502: {"model": ErrorResponse, "description": "The provider failed while resuming the run"},
    504: {"model": ErrorResponse, "description": "The provider did not respond in time"},
}


@router.get(
    "",
    response_model=ApprovalQueue,
    summary="The approval inbox",
    responses={
        401: RESPONSES[401],
        403: RESPONSES[403],
        422: {"model": ErrorResponse, "description": "The page cursor is not valid here"},
    },
)
async def list_approvals(
    membership: RequireMember,
    approvals: ApprovalServiceDep,
    status: Annotated[
        list[ApprovalStatus] | None,
        Query(description="States to include. Repeatable. Omitted means pending only."),
    ] = None,
    tool_name: Annotated[
        str | None,
        Query(max_length=64, description="Restrict to one tool. Matched exactly."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: Annotated[
        uuid.UUID | None,
        Query(description="The id of the last approval on the previous page."),
    ] = None,
) -> ApprovalQueue:
    """One page of this organization's approvals, oldest first.

    Readable by any active member. Seeing that the agent wants to cancel a
    shipment is not the same as being able to let it, and a queue only one
    person can see is a queue nobody chases.

    Defaults to what is *waiting*, because that is what an inbox is for; pass
    ``status`` to look at what has been decided. The page is bounded whatever is
    asked for, ordered by when each approval was requested, and paged from the
    last row seen rather than by offset - so a decision made while somebody is
    paging cannot make the next page skip an item.

    Reading the queue also lapses anything overdue, which is where the absence
    of a background worker shows: this is the moment it is worth paying for a
    sweep, because somebody is about to be offered decisions and none of them
    should be one the decision path would refuse.
    """
    del membership

    page = await approvals.list_queue(
        statuses=tuple(status) if status else (ApprovalStatus.PENDING,),
        tool_name=tool_name,
        limit=limit,
        cursor=cursor,
    )

    return ApprovalQueue(
        approvals=[ApprovalResponse.from_record(record) for record in page],
        # A full page might be the last one; the only way to find out is to ask
        # for the next, which is what the cursor is for. A short page is
        # definitively the end.
        next_cursor=page[-1].id if len(page) == limit else None,
    )


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
    """Read one of this organization's approvals, decided or not.

    Enough to decide on: what is being proposed, which record it affects, why a
    person has to agree, which run is waiting, who asked, when it lapses, and
    what happens either way. None of the things that would make it a leak: no
    arguments, no tool output, no conversation, no prompt, no provider payload,
    no credentials. There is no field on the response for any of them.
    """
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
    decision: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Let the action happen, and carry the paused process forward.

    The tool runs **at most once** for this approval's execution, whatever
    happens above: the decision is taken by a conditional update that only one
    request can win, and the execution is claimed by a second one. A repeated
    approval is a 409, not a second cancellation.

    The response says what was resumed - an agent run, a workflow run, or both
    where an agent step inside a workflow paused - as it stands afterwards.

    An optional ``reason`` may accompany the decision. It is stored with the
    approval and is the only thing about this request that is not already in the
    database; it cannot change what is being approved.
    """
    del membership
    return ApprovalDecisionResponse.from_decision(
        await approvals.decide(
            approval_id, approve=True, reason=decision.reason if decision else None
        )
    )


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
    decision: ApprovalDecisionRequest | None = None,
) -> ApprovalDecisionResponse:
    """Decline the action, and let whatever was waiting say so.

    The tool is never executed. For an agent, the refusal goes back into the
    conversation as an ordinary tool outcome and its own bounded loop decides
    what to tell the user. For a workflow, it takes the path the definition
    declared for a refusal, or stops with ``workflow_approval_rejected`` if it
    declared none. Neither is turned into a failure of the platform, and neither
    is reported as if the action had been performed.

    An optional ``reason`` may accompany the refusal, and is worth more here
    than on an approval: somebody looking at this later wants to know why the
    answer was no.
    """
    del membership
    return ApprovalDecisionResponse.from_decision(
        await approvals.decide(
            approval_id, approve=False, reason=decision.reason if decision else None
        )
    )
