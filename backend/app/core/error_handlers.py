"""Translation of exceptions into HTTP responses.

Registered once on the application. Every error leaves the API in the same
envelope (``app.schemas.common.ErrorResponse``), so clients have one shape to
parse, and every response carries the correlation id.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.exceptions import AppError
from app.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

# Status codes that map onto our stable error codes, for errors raised by
# Starlette or FastAPI rather than by our own services. Written as literals
# because Starlette has renamed some of these constants between versions.
HTTP_UNPROCESSABLE_CONTENT = 422
HTTP_INTERNAL_SERVER_ERROR = 500

_HTTP_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    HTTP_UNPROCESSABLE_CONTENT: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=dict(details or {})),
        request_id=get_request_id(),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers or None,
    )


async def handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    """Expected domain failure: log at the level its severity warrants.

    A 4xx message is guidance for the caller and is returned as written. A 5xx
    message is not: it describes something wrong on this side, and the specific
    text routinely names internal detail - which environment variable is unset,
    which provider is configured, which model was refused. Those go to the log;
    the client gets the class's generic message and the correlation id that
    ties the two together.
    """
    # Narrowing: the handler is only registered for this exception type.
    assert isinstance(exc, AppError)

    if exc.status_code >= 500:
        logger.error("%s: %s", exc.code, exc.message, exc_info=exc)
        # Class default, not the instance's message, and no details.
        return _error_response(exc.status_code, exc.code, type(exc).message, None, exc.headers)

    logger.info("%s: %s", exc.code, exc.message)
    return _error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    """Request failed schema validation, before reaching any endpoint."""
    assert isinstance(exc, RequestValidationError)

    # Pydantic's error objects can contain non-serialisable values in "input".
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]

    return _error_response(
        HTTP_UNPROCESSABLE_CONTENT,
        "validation_error",
        "The request payload is invalid.",
        {"errors": errors},
    )


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    """Errors raised by Starlette or FastAPI itself, such as 404 and 405."""
    assert isinstance(exc, StarletteHTTPException)

    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return _error_response(exc.status_code, code, message)


async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """Anything unhandled.

    The real cause is logged with a traceback but never returned: an unexpected
    exception can carry internal detail that must not reach a client. The
    correlation id in the response is what ties a report back to the log.
    """
    logger.exception("Unhandled exception", exc_info=exc)

    return _error_response(
        HTTP_INTERNAL_SERVER_ERROR,
        "internal_error",
        "An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the application."""
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
