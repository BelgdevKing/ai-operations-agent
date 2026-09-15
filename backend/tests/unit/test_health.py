"""Health endpoints and the readiness rule behind them."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app import __version__
from app.core import cache, database
from app.core.config import Settings
from app.services.health import HealthService


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
def redis_required(app: FastAPI) -> Callable[[bool], None]:
    """Toggle whether Redis counts towards readiness for this application."""

    def set_required(required: bool) -> None:
        app.state.settings.redis_required = required

    return set_required


# -- Liveness -----------------------------------------------------------------


async def test_health_returns_ok(client: AsyncClient, settings: Settings) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": settings.app_name,
        "version": __version__,
        "environment": "test",
    }


async def test_health_does_not_touch_dependencies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness must answer even when PostgreSQL and Redis are down."""

    async def explode() -> bool:
        raise AssertionError("liveness must not call a dependency")

    monkeypatch.setattr(database, "check_database", explode)
    monkeypatch.setattr(cache, "check_redis", explode)

    assert (await client.get("/health")).status_code == 200


# -- Readiness ----------------------------------------------------------------


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
    assert response.json()["status"] == "degraded"


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


# -- Service layer, without HTTP ----------------------------------------------


def test_liveness_reports_the_configured_environment() -> None:
    # A deployed environment refuses the shipped defaults - the JWT secret, the
    # database password and DEBUG - and needs the selected provider's key. Each
    # of those rules is tested where it belongs; here they are only the price of
    # asking what staging reports about itself.
    service = HealthService(
        Settings(
            app_env="staging",
            app_name="Svc",
            jwt_secret_key="s" * 48,
            database_url="postgresql+asyncpg://aiops:real@db:5432/aiops",
            debug=False,
            anthropic_api_key="test-anthropic-secret",
        )
    )

    result = service.liveness()

    assert result.environment == "staging"
    assert result.service == "Svc"
    assert result.status == "ok"


async def test_readiness_rule_ignores_optional_dependencies(
    dependencies: Callable[[bool, bool], None],
) -> None:
    dependencies(True, False)
    service = HealthService(Settings(redis_required=False))

    result = await service.readiness()

    assert result.status == "ready"
    assert result.dependencies["redis"].status == "unavailable"
