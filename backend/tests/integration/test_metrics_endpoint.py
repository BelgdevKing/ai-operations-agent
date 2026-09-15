"""The ``/metrics`` endpoint: off, protected, and carrying no tenant.

The dangerous outcome Part 19 has is an exposition endpoint that answers
anybody, so the tests are weighted that way. Two of them are about the endpoint
existing at all; the rest are about what it refuses and what it does not say.

Everything here builds its own application, because the point is what a
*deployment's configuration* does.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.main import create_app
from tests.integration.agent_helpers import RUN_URL, answering, body, script
from tests.integration.auth_helpers import register

pytestmark = pytest.mark.integration

TOKEN = "metrics-token-for-tests-0123456789"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def deployment(session: AsyncSession, **settings: object) -> tuple[object, AsyncClient]:
    application = create_app(Settings(app_env="test", **settings))
    application.dependency_overrides[get_session] = lambda: session
    return application, AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    )


def enabled(session: AsyncSession) -> tuple[object, AsyncClient]:
    return deployment(session, metrics_enabled=True, metrics_token=TOKEN)


# -- Whether it exists --------------------------------------------------------


async def test_metrics_are_off_by_default(session: AsyncSession) -> None:
    """The default a deployment gets without thinking about it."""
    _, client = deployment(session)

    async with client:
        response = await client.get("/metrics", headers=BEARER)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_a_disabled_endpoint_is_indistinguishable_from_one_that_does_not_exist(
    session: AsyncSession,
) -> None:
    """404 rather than 403: "forbidden" would confirm there is something here."""
    _, client = deployment(session)

    async with client:
        with_token = await client.get("/metrics", headers=BEARER)
        without = await client.get("/metrics")

    assert with_token.status_code == without.status_code == 404
    assert with_token.json() == without.json() | {"request_id": with_token.json()["request_id"]}


def test_a_deployment_cannot_enable_metrics_without_a_token() -> None:
    """Refused at start-up, in every environment, so there is no open mode."""
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        Settings(app_env="test", metrics_enabled=True)


def test_a_short_token_is_refused() -> None:
    """Its only real defence is length; it is compared in constant time."""
    with pytest.raises(ValueError):
        Settings(app_env="test", metrics_enabled=True, metrics_token="short")


# -- Who may read it ----------------------------------------------------------


async def test_a_scrape_without_a_token_is_refused(session: AsyncSession) -> None:
    _, client = enabled(session)

    async with client:
        response = await client.get("/metrics")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_a_wrong_token_is_refused(session: AsyncSession) -> None:
    _, client = enabled(session)

    async with client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


async def test_a_token_without_the_bearer_scheme_is_refused(session: AsyncSession) -> None:
    _, client = enabled(session)

    async with client:
        response = await client.get("/metrics", headers={"Authorization": TOKEN})

    assert response.status_code == 401


async def test_a_user_session_is_not_a_metrics_credential(session: AsyncSession) -> None:
    """Separate credentials on purpose: a scraper has no membership, no
    organization and no role, so authenticating it as a user would mean a
    service account whose whole job is to bypass tenancy."""
    application, client = enabled(session)

    async with client:
        account = await register(client)
        response = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {account.token}"}
        )

    del application
    assert response.status_code == 401


async def test_the_right_token_is_served(session: AsyncSession) -> None:
    _, client = enabled(session)

    async with client:
        response = await client.get("/metrics", headers=BEARER)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE aiops_http_requests_total counter" in response.text


# -- What it says -------------------------------------------------------------


async def test_real_traffic_is_counted(session: AsyncSession) -> None:
    application, client = enabled(session)
    script(application, answering("Done."))

    async with client:
        account = await register(client)
        await client.post(RUN_URL, json=body(), headers=account.headers())
        rendered = (await client.get("/metrics", headers=BEARER)).text

    assert 'aiops_agent_runs_total{status="completed"} 1' in rendered
    expected = (
        'aiops_llm_calls_total{model="claude-opus-5",'
        'operation="generate_structured",outcome="success"} 1'
    )
    assert expected in rendered
    assert "aiops_llm_tokens_total{" in rendered


async def test_the_route_label_is_a_template_not_a_path(session: AsyncSession) -> None:
    """The cardinality defence where it actually matters: one series per route,
    not one per run id."""
    application, client = enabled(session)
    script(application, answering("Done."))

    async with client:
        account = await register(client)
        run = (await client.post(RUN_URL, json=body(), headers=account.headers())).json()
        await client.get(f"/api/v1/ai/runs/{run['run_id']}", headers=account.headers())
        rendered = (await client.get("/metrics", headers=BEARER)).text

    assert "{run_id}" in rendered
    assert run["run_id"] not in rendered, "a run id must never become a label"


async def test_an_unmatched_path_does_not_become_a_label(session: AsyncSession) -> None:
    """A 404's path is whatever somebody typed. That is user input."""
    _, client = enabled(session)

    async with client:
        await client.get("/api/v1/nothing-here-9f3a2b")
        rendered = (await client.get("/metrics", headers=BEARER)).text

    assert "nothing-here-9f3a2b" not in rendered
    assert 'route="unmatched"' in rendered


async def test_the_exposition_names_no_organization_and_no_identifier(
    session: AsyncSession,
) -> None:
    application, client = enabled(session)
    script(application, answering("Done."))

    async with client:
        account = await register(client)
        await client.post(RUN_URL, json=body(), headers=account.headers())
        rendered = (await client.get("/metrics", headers=BEARER)).text

    assert str(account.organization_id) not in rendered
    assert str(account.user_id) not in rendered
    assert "organization_id" not in rendered
    assert account.token not in rendered


async def test_the_exposition_carries_no_business_data(session: AsyncSession) -> None:
    application, client = enabled(session)
    script(application, answering("Done."))

    async with client:
        account = await register(client)
        await client.post(
            RUN_URL,
            json=body("Please cancel shipment ABC123 for Acme."),
            headers=account.headers(),
        )
        rendered = (await client.get("/metrics", headers=BEARER)).text

    for forbidden in ("ABC123", "Acme", "Please cancel", account.email):
        assert forbidden not in rendered, f"a metric must not carry {forbidden!r}"


async def test_two_deployments_measure_independently(session: AsyncSession) -> None:
    """Why the registry is per application: every integration test builds one."""
    first_app, first = enabled(session)
    _, second = enabled(session)
    script(first_app, answering("Done."))

    async with first, second:
        account = await register(first)
        await first.post(RUN_URL, json=body(), headers=account.headers())

        mine = (await first.get("/metrics", headers=BEARER)).text
        theirs = (await second.get("/metrics", headers=BEARER)).text

    assert 'aiops_agent_runs_total{status="completed"} 1' in mine
    assert "aiops_agent_runs_total{" not in theirs


async def test_a_direct_generation_is_metered_once(session: AsyncSession) -> None:
    """Counted at the gateway, and only there.

    ``/ai/generate`` now also writes a durable usage row, and the two are
    different ledgers with different lifetimes - the metric is process-wide and
    tenant-blind, the row is per tenant and permanent. What must not happen is
    the call being *metered* twice because it is now recorded in two places.
    """
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    application, client = enabled(session)
    _install(application, FakeProvider())

    async with client:
        account = await register(client)
        await client.post(GENERATE, json=a_body(), headers=account.headers())
        rendered = (await client.get("/metrics", headers=BEARER)).text

    expected = (
        'aiops_llm_calls_total{model="claude-opus-5",operation="generate",outcome="success"} 1'
    )
    assert expected in rendered, "one call, counted once"
    assert 'aiops_llm_tokens_total{model="claude-opus-5",direction="input"} 23' in rendered


async def test_a_durable_usage_row_puts_nothing_new_into_the_metrics(
    session: AsyncSession,
) -> None:
    """The new table is tenant data; the exposition must stay tenant-blind."""
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    application, client = enabled(session)
    _install(application, FakeProvider())

    async with client:
        account = await register(client)
        await client.post(GENERATE, json=a_body(), headers=account.headers())
        rendered = (await client.get("/metrics", headers=BEARER)).text

    assert str(account.organization_id) not in rendered
    assert str(account.user_id) not in rendered
    assert "generation_usage" not in rendered
    assert "ABC123" not in rendered and "Acme" not in rendered
