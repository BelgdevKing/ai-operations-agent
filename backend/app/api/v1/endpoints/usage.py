"""What this organization used, and what it cost.

    GET /ai/usage    any active member may read it

**One endpoint, not five.** ``/usage/by-model``, ``/usage/by-tool`` and
``/usage/by-day`` would be the same query, the same authorization and the same
tenant filter written three more times; the breakdowns are fields of one report
instead. The per-day series is the only one that costs an extra query, so it is
the only one behind a flag.

**Readable by any active member.** Knowing how much the organization is
spending is not the same as being able to spend it, and a figure only
administrators can see is a figure nobody checks. Nothing here can change
anything.

**Bounded, twice.** The window is capped by configuration and refused if it is
longer, and every breakdown is a ``GROUP BY`` with a ``LIMIT``. There is no
parameter that makes this return rows rather than sums.

What the response cannot contain, because no field exists for it: prompts,
answers, tool arguments, tool results, conversation content, idempotency keys,
credentials. The report is built from execution records, which never held any of
those.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import RequireMember, UsageServiceDep
from app.schemas.common import ErrorResponse
from app.schemas.usage import UsageResponse

router = APIRouter()

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "Not a member of an active organization"},
    422: {
        "model": ErrorResponse,
        "description": "The requested window is not one this API will aggregate",
    },
}


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="Usage and estimated cost for this organization",
    responses=RESPONSES,
)
async def usage(
    membership: RequireMember,
    service: UsageServiceDep,
    since: Annotated[
        datetime | None,
        Query(
            description="Start of the window. Defaults to the configured span back from 'until'."
        ),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="End of the window, exclusive. Defaults to now."),
    ] = None,
    include_days: Annotated[
        bool,
        Query(description="Include the per-day breakdown. One extra query; off by default."),
    ] = False,
) -> UsageResponse:
    """Aggregate this organization's execution records over a window.

    Derived from the durable records the platform already keeps - agent steps,
    runs, tool executions, workflow runs and approvals - rather than from a
    separate usage ledger. There is one source of truth for what happened, and
    this reads it.

    **Cost is reported only where the deployment has configured a price.** A
    model with no price contributes its tokens and no money, and the response
    says how many calls that covered. A deployment with no price book reports
    complete usage with the cost explicitly unknown - never zero.

    The organization is the caller's own, taken from the verified membership.
    There is no parameter that names one.
    """
    del membership

    report = await service.report(since=since, until=until, include_days=include_days)
    return UsageResponse.from_report(report)
