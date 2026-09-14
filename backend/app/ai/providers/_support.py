"""Helpers shared by the provider adapters.

Deliberately small. Error translation is written out per adapter instead of
being generalised here: the two SDKs expose similarly *named* exception classes
from unrelated hierarchies, so a shared mapper would have to be parameterised
with both sets and would end up harder to read than two explicit tables.
"""

from __future__ import annotations

import time


class Stopwatch:
    """Measures wall-clock duration of a provider call.

    Latency is measured here rather than taken from the provider, because not
    every provider reports it and the number that matters is what the caller
    waited for - including transport and SDK retries.
    """

    def __init__(self) -> None:
        self._started = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000


def request_id_of(response: object) -> str | None:
    """Best available identifier for a call, for support requests.

    Both SDKs expose the ``request-id`` response header as ``_request_id``;
    despite the underscore it is public API. It can be absent, so the object's
    own id is the fallback - always present and still enough to find the call
    in a provider dashboard.
    """
    request_id = getattr(response, "_request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id

    fallback = getattr(response, "id", None)
    return fallback if isinstance(fallback, str) and fallback else None


def retry_after_seconds(error: object) -> float | None:
    """Seconds a rate-limited caller should wait, when the provider says so.

    Read from the ``retry-after`` header rather than guessed, so backing off
    uses the provider's own number.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        # The header also permits an HTTP date, which is not worth parsing for
        # a hint; the caller falls back to its own backoff.
        return None
