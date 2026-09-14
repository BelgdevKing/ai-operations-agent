"""Health endpoint behaviour. No external services required."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from httpx import AsyncClient

from app import __version__
from app.core import cache, database
from app.core.config import get_settings


def _returns(value: bool) -> Callable[[], object]:
    async def _check() -> bool:
        return value

    return _check


@pytest.fixture
def dependencies(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool, bool], None]:
    """Force the outcome of both readiness checks."""

    def set_state(postgres_ok: bool, redis_ok: bool) -> None:
        monkeypatch.setattr(database, "check_database", _returns(postgres_ok))
        monkeypatch.setattr(cache, "check_redis", _returns(redis_ok))

    return set_state


@pytest.fixture
def redis_required(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Toggle whether Redis counts towards readiness."""

    def set_required(required: bool) -> None:
        monkeypatch.setattr(get_settings(), "redis_required", required)

    return set_required


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["service"]
    assert body["environment"]


async def test_health_does_not_touch_dependencies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must answer even when PostgreSQL and Redis are down."""

    async def explode() -> bool:
        raise AssertionError("liveness must not call a dependency")

    monkeypatch.setattr(database, "check_database", explode)
    monkeypatch.setattr(cache, "check_redis", explode)

    assert (await client.get("/health")).status_code == 200


async def test_readiness_ok_when_everything_is_up(
    client: AsyncClient,
    dependencies: Callable[[bool, bool], None],
    redis_required: Callable[[bool], None],
) -> None:
    dependencies(True, True)
    redis_required(True)

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {
            "postgres": {"status": "ok", "required": True},
            "redis": {"status": "ok", "required": True},
        },
    }


async def test_readiness_ok_when_optional_redis_is_down(
    client: AsyncClient,
    dependencies: Callable[[bool, bool], None],
    redis_required: Callable[[bool], None],
) -> None:
    """Native development: no Redis installed, service still ready."""
    dependencies(True, False)
    redis_required(False)

    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["dependencies"]["redis"] == {"status": "unavailable", "required": False}
    assert body["dependencies"]["postgres"] == {"status": "ok", "required": True}


async def test_readiness_degraded_when_required_redis_is_down(
    client: AsyncClient,
    dependencies: Callable[[bool, bool], None],
    redis_required: Callable[[bool], None],
) -> None:
    """Compose stack: Redis is part of the stack, so its absence is a fault."""
    dependencies(True, False)
    redis_required(True)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["redis"] == {"status": "unavailable", "required": True}


async def test_readiness_degraded_when_postgres_is_down(
    client: AsyncClient,
    dependencies: Callable[[bool, bool], None],
    redis_required: Callable[[bool], None],
) -> None:
    """PostgreSQL is always required, whatever Redis is doing."""
    dependencies(False, True)
    redis_required(False)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["postgres"] == {"status": "unavailable", "required": True}


async def test_root_returns_service_metadata(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.json()["health"] == "/health"


async def test_unknown_route_returns_404(client: AsyncClient) -> None:
    assert (await client.get("/does-not-exist")).status_code == 404
