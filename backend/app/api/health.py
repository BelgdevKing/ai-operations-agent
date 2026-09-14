"""Health endpoints.

``/health``       liveness  - answers without touching any dependency.
``/health/ready`` readiness - reports PostgreSQL and Redis, 503 when degraded.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app import __version__
from app.core import cache, database
from app.core.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


class DependencyCheck(BaseModel):
    """Outcome for a single dependency.

    ``required`` says whether an unavailable dependency makes the service
    unready. Redis is optional until a feature actually needs it.
    """

    status: str
    required: bool


class ReadinessResponse(BaseModel):
    status: str
    dependencies: dict[str, DependencyCheck]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Report that the process is up. Used by the container health check."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable"}},
)
async def readiness(response: Response) -> ReadinessResponse:
    """Report whether the dependencies needed to serve traffic are reachable.

    Only required dependencies affect the status code. PostgreSQL is always
    required; Redis follows ``REDIS_REQUIRED``, which is false for native
    development and true in the Compose stack.
    """
    settings = get_settings()

    postgres_ok = await database.check_database()
    redis_ok = await cache.check_redis()

    dependencies = {
        "postgres": DependencyCheck(
            status="ok" if postgres_ok else "unavailable", required=True
        ),
        "redis": DependencyCheck(
            status="ok" if redis_ok else "unavailable",
            required=settings.redis_required,
        ),
    }

    ready = all(check.status == "ok" for check in dependencies.values() if check.required)

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "degraded",
        dependencies=dependencies,
    )
