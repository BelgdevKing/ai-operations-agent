"""Domain exception hierarchy.

Services raise these; the API layer translates them into HTTP responses in
``app.core.error_handlers``. Nothing below the API layer imports FastAPI or
``HTTPException`` — that is what keeps the service layer transport-agnostic.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """Base class for every expected, handled application error.

    Attributes:
        status_code: HTTP status the API layer should return.
        code: Stable machine-readable identifier for clients.
        message: Human-readable description, safe to expose.
        details: Optional structured context, safe to expose.
        headers: Response headers the status requires.
    """

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    # Response headers the status requires, such as WWW-Authenticate on a 401.
    # A class attribute, so a subclass declares them once.
    headers: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.code = code or type(self).code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    """A requested resource does not exist, or is not visible to the caller."""

    status_code = 404
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    """The request conflicts with the current state of the resource."""

    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state."


class ValidationError(AppError):
    """Input is well-formed but fails a business rule.

    Schema-level failures are raised by Pydantic and handled separately; this
    is for rules the schema cannot express.
    """

    status_code = 422
    code = "validation_error"
    message = "The request could not be processed."


class UnauthorizedError(AppError):
    """The caller is not authenticated."""

    status_code = 401
    code = "unauthorized"
    message = "Authentication is required."


class PermissionDeniedError(AppError):
    """The caller is authenticated but not allowed to perform this action."""

    status_code = 403
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class ServiceUnavailableError(AppError):
    """A dependency the request needs is unavailable."""

    status_code = 503
    code = "service_unavailable"
    message = "A required dependency is unavailable."
