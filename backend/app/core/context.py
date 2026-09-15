"""Request-scoped context.

Context variables carry the ids for the current request so that every log
record can be tied back to it without threading an argument through every call.

Two ids, with different jobs and different lifetimes. The **correlation id** is
this application's own: always present, returned to the client as
``X-Request-ID``, and what somebody quotes when they report a problem. The
**trace id** belongs to a distributed trace that may have started in another
service entirely; it is set only while tracing is enabled, so a deployment that
has not turned tracing on produces exactly the log records it did before.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_request_id() -> str | None:
    """Return the current request's correlation id, if there is one."""
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """Bind a correlation id to the current context."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the previous correlation id."""
    _request_id.reset(token)


def get_trace_id() -> str | None:
    """Return the current request's trace id, if tracing is on."""
    return _trace_id.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    """Bind a trace id to the current context."""
    return _trace_id.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    """Restore the previous trace id."""
    _trace_id.reset(token)
