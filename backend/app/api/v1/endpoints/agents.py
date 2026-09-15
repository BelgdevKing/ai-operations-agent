"""Running an agent, and reading a run back.

As thin as the generation endpoint, and for the same reason: it authenticates,
establishes the tenant, converts the request into internal types, and hands off.
It does not pick a provider, retry, interpret a decision or execute anything.

    request -> auth + tenant -> AgentExecutionService -> AgentRuntime -> gateway
                                        |
                                        v
                                   PostgreSQL

Mounted under the existing ``/ai`` prefix rather than as a second AI API, so
everything the generation endpoint established - the error envelope, the
membership requirement, the correlation id - applies here unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header

from app.api.deps import AgentExecutionServiceDep, AgentRegistryDep, RequireMember
from app.core.context import get_request_id
from app.schemas.agent import (
    IDEMPOTENCY_KEY_PATTERN,
    AgentRunRequest,
    AgentRunResponse,
    AgentSummary,
)
from app.schemas.common import ErrorResponse

router = APIRouter()

IDEMPOTENCY_HEADER = "Idempotency-Key"

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "Not a member of an active organization"},
    404: {"model": ErrorResponse, "description": "No such agent or conversation"},
    409: {"model": ErrorResponse, "description": "The agent is disabled"},
    413: {"model": ErrorResponse, "description": "The conversation grew too large to continue"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
    429: {"model": ErrorResponse, "description": "The provider is rate limiting; retry shortly"},
    502: {
        "model": ErrorResponse,
        "description": "The provider failed, or the agent's output was unusable",
    },
    504: {"model": ErrorResponse, "description": "The provider did not respond in time"},
}

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias=IDEMPOTENCY_HEADER,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "Opaque token making this run repeatable. Sending the same key again "
            "for the same organization returns the run it already created rather "
            "than starting a second one - which is what makes a retry safe when "
            "the first answer never arrived. Keys are per organization: the same "
            "value in a different organization is a different run."
        ),
    ),
]


@router.get(
    "/agents",
    response_model=list[AgentSummary],
    summary="Agents this organization can run",
    responses={
        401: RESPONSES[401],
        403: RESPONSES[403],
    },
)
async def list_agents(
    membership: RequireMember,
    registry: AgentRegistryDep,
) -> list[AgentSummary]:
    """List the agents available to the caller's organization.

    Scoped to the caller's own tenant, so another organization's agents are not
    merely hidden from the response - they are never looked up.

    Depends on the registry rather than the runtime, and that is not cosmetic:
    the runtime holds the LLM gateway, which is built on first use and fails
    without a provider credential. Listing what a deployment offers is not a
    model call, and answering it with a 500 about a provider would be a lie
    about what went wrong.
    """
    agents = registry.list_for(membership.organization_id)
    return [
        AgentSummary(id=agent.id, name=agent.name, description=agent.description)
        for agent in agents
    ]


@router.post(
    "/agents/{agent_id}/run",
    response_model=AgentRunResponse,
    summary="Run an agent over a conversation",
    responses=RESPONSES,
)
async def run_agent(
    agent_id: uuid.UUID,
    payload: AgentRunRequest,
    membership: RequireMember,
    execution: AgentExecutionServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> AgentRunResponse:
    """Execute one agent run, durably.

    The organization is the caller's verified membership - there is no field in
    the request that could name another, and an agent belonging to a different
    tenant is reported as not found.

    The agent's instructions, model and limits are all server configuration.
    The body carries the conversation and nothing else; anything extra is a 422.

    **The run may not finish.** If the agent asks for a tool that needs a person,
    the run is left ``awaiting_approval`` with the pending decision described in
    ``approval``, and continues - as the same run - once somebody decides. The
    response is projected from the stored run either way.

    ``membership`` is declared before ``execution`` on purpose: FastAPI resolves
    dependencies in order and the gateway is built on first use, so authorizing
    first is what keeps a provider misconfiguration from answering an anonymous
    caller.
    """
    view = await execution.start(
        agent_id,
        [message.to_llm_message() for message in payload.messages],
        conversation_id=payload.conversation_id,
        idempotency_key=idempotency_key,
        request_id=get_request_id(),
    )
    return AgentRunResponse.from_view(view)


@router.get(
    "/runs/{run_id}",
    response_model=AgentRunResponse,
    summary="One agent run",
    responses={
        401: RESPONSES[401],
        403: RESPONSES[403],
        404: RESPONSES[404],
    },
)
async def get_run(
    run_id: uuid.UUID,
    membership: RequireMember,
    execution: AgentExecutionServiceDep,
) -> AgentRunResponse:
    """Read one of this organization's runs.

    The durable record, which is what makes a run survive the request that
    started it: an interface that was closed while an approval was pending can
    come back and find the run exactly where it was left.
    """
    del membership
    return AgentRunResponse.from_view(await execution.get(run_id))


@router.get(
    "/runs",
    response_model=list[AgentRunResponse],
    summary="Recent agent runs",
    responses={
        401: RESPONSES[401],
        403: RESPONSES[403],
    },
)
async def list_runs(
    membership: RequireMember,
    execution: AgentExecutionServiceDep,
    limit: int = 20,
) -> list[AgentRunResponse]:
    """This organization's runs, newest first."""
    del membership
    views = await execution.list_recent(limit=max(1, min(limit, 100)))
    return [AgentRunResponse.from_view(view) for view in views]
