"""Real connectivity to PostgreSQL and Redis.

Skipped when the services are unreachable, so the suite still runs on a bare
checkout. Inside the Compose stack both checks run for real:

    docker compose exec backend pytest
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core import cache, database

pytestmark = pytest.mark.integration


async def test_postgres_is_reachable() -> None:
    if not await database.check_database():
        pytest.skip("PostgreSQL is not reachable")
    assert await database.check_database() is True


async def test_redis_is_reachable() -> None:
    if not await cache.check_redis():
        pytest.skip("Redis is not reachable")
    assert await cache.check_redis() is True


async def test_readiness_endpoint_against_live_dependencies(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    dependencies = response.json()["dependencies"]

    if response.status_code == 503:
        pytest.skip(f"A required dependency is unavailable: {dependencies}")

    # Ready implies every required dependency answered. Redis may legitimately
    # be absent here, since it is optional in native development.
    assert response.status_code == 200
    assert dependencies["postgres"] == {"status": "ok", "required": True}
    for name, check in dependencies.items():
        assert check["status"] == "ok" or not check["required"], name
