"""HTTP middleware.

Assigns a correlation id to every request, binds it to the logging context,
records the request against the metric registry, logs it once it completes, and
returns the id as ``X-Request-ID`` so a client report can be matched to server
logs.

**The route label is the route template, never the path.** ``/api/v1/ai/runs/{run_id}``
is one time series; ``/api/v1/ai/runs/<a uuid>`` would be one series per run. A
request that matched no route is labelled ``unmatched`` for the same reason -
the path of a 404 is whatever somebody typed, and that is user input going
straight into a metric label.

Written as pure ASGI rather than ``BaseHTTPMiddleware``: the latter runs the
downstream app in a separate task, which breaks context propagation and
interferes with streaming responses and background tasks.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import set_request_id
from app.observability.instruments import Instruments, NullInstruments

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Probe endpoints are polled every few seconds; logging them buries real traffic.
_QUIET_PATHS = frozenset({"/health", "/health/ready"})

UNMATCHED_ROUTE = "unmatched"
"""Label for a request that matched no route. Bounded, unlike the path."""


class RequestContextMiddleware:
    """Correlation id, timing, and one access log line per request."""

    def __init__(self, app: ASGIApp, instruments: Instruments | None = None) -> None:
        self.app = app
        # Null by default so an application assembled without instruments -
        # which is any test that builds the middleware directly - still serves.
        self.instruments = instruments or NullInstruments()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Honour an id supplied upstream (proxy, another service) if present.
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming or uuid.uuid4().hex

        # Deliberately not reset afterwards. The handler that builds a 500
        # response runs outside this middleware but in the same context, so it
        # still needs the id. Each request sets it again on entry, and the
        # server gives each request task its own context copy, so nothing leaks.
        set_request_id(request_id)

        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            elapsed = time.perf_counter() - started
            duration_ms = round(elapsed * 1000, 2)

            # Read after the call, because routing is what populates it.
            self.instruments.record_http(
                method=scope.get("method", "-"),
                route=_route_template(scope),
                status_code=status_code,
                seconds=elapsed,
            )

            if scope.get("path") not in _QUIET_PATHS:
                logger.info(
                    "%s %s %s",
                    scope.get("method", "-"),
                    scope.get("path", "-"),
                    status_code,
                    extra={
                        "context": {
                            "method": scope.get("method"),
                            "path": scope.get("path"),
                            "status_code": status_code,
                            "duration_ms": duration_ms,
                        }
                    },
                )


def _route_template(scope: Scope) -> str:
    """The matched route's path template, or a bounded stand-in.

    ``scope["route"]`` is set by Starlette's router once a request has matched.
    Anything that did not match - a 404, a malformed path, a scanner - has no
    template, and its raw path must not become a label.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) and template else UNMATCHED_ROUTE
