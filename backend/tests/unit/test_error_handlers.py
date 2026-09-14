"""Exception handling: every error leaves the API in the same envelope."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationError,
)
from app.core.middleware import REQUEST_ID_HEADER


@pytest.fixture
def app_with_failing_routes(app: FastAPI) -> FastAPI:
    """Attach routes that fail in each way the handlers cover."""

    @app.get("/raises/not-found")
    async def _not_found() -> None:
        raise NotFoundError("Tenant does not exist.", details={"tenant_id": "abc"})

    @app.get("/raises/conflict")
    async def _conflict() -> None:
        raise ConflictError()

    @app.get("/raises/unhandled")
    async def _unhandled() -> None:
        raise RuntimeError("database on fire; connection string is postgres://secret")

    @app.get("/raises/needs-query")
    async def _needs_query(count: int) -> dict[str, int]:
        return {"count": count}

    return app


async def test_domain_error_becomes_its_status_and_code(
    app_with_failing_routes: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/raises/not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "Tenant does not exist."
    assert body["error"]["details"] == {"tenant_id": "abc"}


async def test_error_carries_the_correlation_id(
    app_with_failing_routes: FastAPI, client: AsyncClient
) -> None:
    """The id in the body must match the header, or reports cannot be traced."""
    response = await client.get("/raises/conflict")

    assert response.status_code == 409
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_domain_error_uses_its_default_message(
    app_with_failing_routes: FastAPI, client: AsyncClient
) -> None:
    body = (await client.get("/raises/conflict")).json()

    assert body["error"]["code"] == "conflict"
    assert body["error"]["message"] == ConflictError.message
    assert body["error"]["details"] == {}


async def test_unknown_route_uses_the_same_envelope(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_schema_validation_reports_the_offending_field(
    app_with_failing_routes: FastAPI, client: AsyncClient
) -> None:
    response = await client.get("/raises/needs-query", params={"count": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    fields = [error["field"] for error in body["error"]["details"]["errors"]]
    assert "query.count" in fields


async def test_unhandled_exception_does_not_leak_internals(
    app_with_failing_routes: FastAPI,
    client_factory: Callable[..., AsyncClient],
) -> None:
    """A 500 must expose nothing beyond a generic message and the request id."""
    async with client_factory(raise_app_exceptions=False) as client:
        response = await client.get("/raises/unhandled")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "An unexpected error occurred."
    assert "postgres://secret" not in response.text
    assert "RuntimeError" not in response.text
    assert body["request_id"]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (NotFoundError, 404, "not_found"),
        (ConflictError, 409, "conflict"),
        (ValidationError, 422, "validation_error"),
        (UnauthorizedError, 401, "unauthorized"),
        (PermissionDeniedError, 403, "permission_denied"),
        (ServiceUnavailableError, 503, "service_unavailable"),
    ],
)
def test_error_classes_declare_their_contract(
    error: type[AppError], expected_status: int, expected_code: str
) -> None:
    assert error.status_code == expected_status
    assert error.code == expected_code
    assert issubclass(error, AppError)


def test_error_accepts_overrides() -> None:
    error = AppError("custom", code="custom_code", details={"k": "v"})

    assert error.message == "custom"
    assert error.code == "custom_code"
    assert error.details == {"k": "v"}
    assert str(error) == "custom"
