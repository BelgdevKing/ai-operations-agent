"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Settings for the test process, isolated from any .env on disk.

    Argon2 runs at reduced cost here: at production parameters every
    registration and login in the suite would take ~60ms of deliberate work.
    The algorithm and code path are unchanged - only how hard it is - and
    tests/unit/test_security.py asserts the real parameters directly.
    """
    return Settings(
        app_env="test",
        log_format="console",
        redis_required=False,
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A freshly built application, so no test inherits another's routes."""
    return create_app(settings)


@pytest.fixture
def client_factory(app: FastAPI) -> Callable[..., AsyncClient]:
    """Build a client against the app.

    ``raise_app_exceptions=False`` is needed to observe the 500 response an
    unhandled exception produces; Starlette re-raises it otherwise.
    """

    def build(*, raise_app_exceptions: bool = True) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://testserver",
        )

    return build


@pytest.fixture
async def client(client_factory: Callable[..., AsyncClient]) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound to the ASGI app in-process (no network, no lifespan)."""
    async with client_factory() as async_client:
        yield async_client


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Keep the cached settings singleton from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
