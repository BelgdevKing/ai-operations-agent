"""Request-scoped context.

A context variable carries the correlation id for the current request so that
every log record can be tied back to it without threading an argument through
every call. Tenant and user identity will join it in the multi-tenancy phase.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the current request's correlation id, if there is one."""
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind a correlation id to the current context."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the previous correlation id."""
    _request_id.reset(token)
