"""HTTP middleware.

Assigns a correlation id to every request, binds it to the logging context, logs
the request once it completes, and returns it as ``X-Request-ID`` so a client
report can be matched to server logs.

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

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

# Probe endpoints are polled every few seconds; logging them buries real traffic.
_QUIET_PATHS = frozenset({"/health", "/health/ready"})


class RequestContextMiddleware:
    """Correlation id, timing, and one access log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

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
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
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
