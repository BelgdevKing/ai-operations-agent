"""The metrics exposition endpoint.

    GET /metrics        off by default; a shared token when it is on

Outside the versioned API, beside the health probes, for the same reason they
are: a scraper should not have to track an API version, and neither of these is
part of the product's contract with its users.

**Three properties, in the order they matter.**

*It does not exist unless a deployment says so.* ``METRICS_ENABLED`` defaults to
false and a disabled deployment answers 404 rather than 403 - there is nothing
to find, and saying "forbidden" would confirm there was.

*It is never open.* Settings refuse to build with metrics enabled and no token,
so there is no configuration in which this serves anonymously. The token is
compared with :func:`secrets.compare_digest`, which does not leak its length
through timing.

*It carries no tenant data.* Not "carefully filtered tenant data" - none. Every
label in the registry comes from a closed vocabulary (see
:mod:`app.observability.names`), and ``organization_id`` is not among them. An
operator who may watch the fleet but may not read any customer's business is
exactly who this endpoint is for, and the safety is structural rather than a
rule somebody has to follow.

A **separate** credential from the user session on purpose. The caller is a
scraper, not a person: it has no membership, no organization to be scoped to
and no role, so authenticating it as a user would mean inventing a service
account whose whole job is to bypass tenancy. Reusing the bearer *transport* -
``Authorization: Bearer <token>`` - while keeping the credential distinct is the
narrower arrangement.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request, Response

from app.api.deps import SettingsDep
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.observability.instruments import Instruments

router = APIRouter(tags=["metrics"], include_in_schema=False)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

BEARER = "bearer"


class MetricsNotEnabledError(NotFoundError):
    """Metrics are off for this deployment.

    A 404 rather than a 403: a disabled endpoint should be indistinguishable
    from one that was never built.
    """

    code = "not_found"
    message = "Not found."


class MetricsUnauthorizedError(UnauthorizedError):
    """Wrong token, or none.

    One error for both, with one message. "No token" and "wrong token" are the
    same answer here; distinguishing them tells a caller which half to work on.
    """

    code = "unauthorized"
    message = "Authentication is required for this endpoint."


@router.get("/metrics", summary="Prometheus exposition", response_class=Response)
async def metrics(request: Request, settings: SettingsDep) -> Response:
    """Render this process's metrics.

    Process-wide figures only. Nothing here is scoped to an organization,
    because nothing here *has* an organization - per-tenant usage is a
    different endpoint, with a session, a membership and a tenant filter.
    """
    if not settings.metrics_enabled or not settings.metrics_token:
        raise MetricsNotEnabledError()

    _authorize(request.headers.get("authorization"), settings.metrics_token)

    instruments: Instruments | None = getattr(request.app.state, "instruments", None)
    body = instruments.render() if instruments is not None else ""

    return Response(content=body, media_type=CONTENT_TYPE)


def _authorize(header: str | None, expected: str) -> None:
    """Check the presented bearer token in constant time."""
    if not header:
        raise MetricsUnauthorizedError()

    scheme, _, presented = header.partition(" ")
    if scheme.lower() != BEARER or not presented:
        raise MetricsUnauthorizedError()

    # compare_digest rather than ==: a plain comparison returns as soon as two
    # bytes differ, which is enough to recover a secret one character at a time.
    if not secrets.compare_digest(presented, expected):
        raise MetricsUnauthorizedError()
