"""The first application-facing AI endpoint.

Deliberately thin. It authenticates, converts the request into the internal
message type, calls the service, and shapes the answer. It does not choose a
provider, retry, catch a provider failure or parse a provider response - all of
that lives below it, and none of it is visible from here.

    request -> auth + tenant context -> AIService -> LLMGateway -> provider
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AIServiceDep, RequireMember
from app.core.context import get_request_id
from app.schemas.ai import AIUsage, GenerateRequest, GenerateResponse
from app.schemas.common import ErrorResponse

router = APIRouter()

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "Not a member of an active organization"},
    422: {"model": ErrorResponse, "description": "Invalid request, or a model that is not allowed"},
    429: {"model": ErrorResponse, "description": "The provider is rate limiting; retry shortly"},
    502: {"model": ErrorResponse, "description": "The provider failed or returned unusable output"},
    504: {"model": ErrorResponse, "description": "The provider did not respond in time"},
}


@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="Generate a completion",
    responses=RESPONSES,
)
async def generate(
    payload: GenerateRequest,
    membership: RequireMember,
    service: AIServiceDep,
) -> GenerateResponse:
    """Ask the configured model for a completion.

    Requires an access token. The organization the call is attributed to comes
    from the caller's verified membership - there is no field in the request
    that could name a different one, and the provider is fixed by server
    configuration rather than chosen per request.

    `model` is optional; omitting it uses the deployment's configured model,
    which is the normal case. Naming one that the deployment has not allowed is
    a 422.

    The parameter order matters and is not cosmetic. FastAPI resolves
    dependencies in the order they are declared, and the gateway is built on
    first use - so with `service` first, a deployment missing its provider
    credential answered an *anonymous* request with a configuration 500 before
    authentication ever ran. Authorization is declared first so the pipeline is
    always: authenticate, authorize, then touch the provider.

    Provider failures arrive already normalised: 429 when rate limited, 504 on
    a timeout, 502 for a provider fault, 500 for a misconfiguration - each in
    the standard error envelope with a correlation id, and never carrying the
    provider's own message.
    """
    response = await service.generate(
        [message.to_llm_message() for message in payload.messages],
        model=payload.model,
        temperature=payload.temperature,
        max_output_tokens=payload.max_output_tokens,
        organization_id=membership.organization_id,
        user_id=membership.user_id,
        request_id=get_request_id(),
    )

    return GenerateResponse(
        content=response.content,
        model=response.model,
        usage=AIUsage.model_validate(response.usage),
        latency_ms=response.latency_ms,
    )
