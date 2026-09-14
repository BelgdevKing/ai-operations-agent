"""Health and readiness logic.

Kept out of the router so the rule for what counts as "ready" is testable
without HTTP, and so the endpoint stays a thin translation layer.
"""

from __future__ import annotations

from app import __version__
from app.core import cache, database
from app.core.config import Settings
from app.schemas.health import DependencyCheck, HealthResponse, ReadinessResponse


class HealthService:
    """Reports process liveness and dependency readiness."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def liveness(self) -> HealthResponse:
        """Describe the running process. Touches no dependency."""
        return HealthResponse(
            status="ok",
            service=self.settings.app_name,
            version=__version__,
            environment=self.settings.app_env,
        )

    async def readiness(self) -> ReadinessResponse:
        """Check every dependency and decide whether the service is ready.

        Only required dependencies affect the verdict. PostgreSQL is always
        required; Redis follows ``REDIS_REQUIRED``, which is false for native
        development and true in the Compose stack.
        """
        postgres_ok = await database.check_database()
        redis_ok = await cache.check_redis()

        dependencies = {
            "postgres": DependencyCheck(
                status="ok" if postgres_ok else "unavailable",
                required=True,
            ),
            "redis": DependencyCheck(
                status="ok" if redis_ok else "unavailable",
                required=self.settings.redis_required,
            ),
        }

        ready = all(check.status == "ok" for check in dependencies.values() if check.required)

        return ReadinessResponse(
            status="ready" if ready else "degraded",
            dependencies=dependencies,
        )
