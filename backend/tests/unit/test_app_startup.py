"""Application startup: the factory, the route table, and the lifespan."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from app import __version__
from app.core.config import Settings
from app.core.middleware import RequestContextMiddleware
from app.main import create_app, lifespan


def _paths(app: FastAPI) -> set[str]:
    """Every path the application actually serves.

    Read from the OpenAPI schema rather than by walking ``app.routes``:
    FastAPI nests included routers, so a flat walk silently misses them.
    """
    return set(app.openapi()["paths"])


def test_create_app_builds_an_application(settings: Settings) -> None:
    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.title == settings.app_name
    assert app.version == __version__


def test_create_app_uses_the_settings_it_is_given() -> None:
    settings = Settings(app_name="Custom Name", api_v1_prefix="/api/v2")
    app = create_app(settings)

    assert app.title == "Custom Name"
    assert app.state.settings is settings


def test_each_application_is_independent(settings: Settings) -> None:
    """The factory must not share state, or tests would contaminate each other."""
    first = create_app(settings)
    second = create_app(settings)

    assert first is not second
    assert first.routes is not second.routes


def test_probe_routes_are_registered(app: FastAPI) -> None:
    paths = _paths(app)

    assert "/health" in paths
    assert "/health/ready" in paths
    assert "/" in paths


def test_probes_sit_outside_the_versioned_api(app: FastAPI, settings: Settings) -> None:
    """Health checks must not move when the API version changes.

    Asserted for the probes specifically: business endpoints are versioned and
    do live under the prefix.
    """
    probes = {path for path in _paths(app) if path.startswith("/health")}

    assert probes == {"/health", "/health/ready"}
    assert not any(path.startswith(settings.api_v1_prefix) for path in probes)


def test_request_context_middleware_is_installed(app: FastAPI) -> None:
    assert any(m.cls is RequestContextMiddleware for m in app.user_middleware)


def test_exception_handlers_are_registered(app: FastAPI) -> None:
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.core.exceptions import AppError

    assert AppError in app.exception_handlers
    assert StarletteHTTPException in app.exception_handlers
    assert Exception in app.exception_handlers


def test_openapi_schema_generates(app: FastAPI) -> None:
    """A malformed response model would surface here rather than at runtime."""
    schema = app.openapi()

    assert schema["info"]["version"] == __version__
    assert "/health" in schema["paths"]


async def test_lifespan_runs_without_dependencies(settings: Settings) -> None:
    """Start-up and shut-down must not require PostgreSQL or Redis to be up."""
    app = create_app(settings)

    async with lifespan(app):
        pass  # Entering and leaving cleanly is the assertion.


async def test_root_reports_service_metadata(client: AsyncClient, settings: Settings) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == settings.app_name
    assert body["version"] == __version__
    assert body["health"] == "/health"
    assert body["api"] == settings.api_v1_prefix
