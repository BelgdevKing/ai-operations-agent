"""Probe endpoints.

Deliberately outside the versioned API: orchestrators and container health
checks should not have to track an API version to find them.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import HealthServiceDep
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(service: HealthServiceDep) -> HealthResponse:
    """Report that the process is up. Used by the container health check."""
    return service.liveness()


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={
        503: {
            "model": ReadinessResponse,
            "description": "A required dependency is unavailable",
        }
    },
)
async def readiness(service: HealthServiceDep, response: Response) -> ReadinessResponse:
    """Report whether the dependencies needed to serve traffic are reachable."""
    result = await service.readiness()

    if result.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return result
