"""The usage API: what an organization used, and whose usage it is.

The figures come from the execution records Parts 16-18 already write, so most
of this file is arithmetic - run an agent, run a workflow, cancel a shipment,
then check the report says so. The assertions that matter more are the other
kind:

**Organization A's report must never include organization B's rows.** Not
merely "must not list them" - the *totals* must not include them either, which
is the version of tenant isolation an aggregate can get wrong quietly.

**A window is always bounded**, because these are aggregates over a
``(organization_id, created_at)`` index and an unbounded one is the single
query shape that would walk a tenant's whole history.

**Cost is never invented.** An unpriced model reports tokens and no money, and
the response says how many calls that covered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_gateway
from app.core.config import Settings
from app.core.database import get_session
from app.main import create_app
from app.models.enums import MemberRole
from tests.integration.agent_helpers import RUN_URL, answering, body, script
from tests.integration.auth_helpers import add_member, register
from tests.integration.test_approval_workflow import pause_a_run
from tests.integration.workflow_helpers import lookup_workflow, published, seed, start

pytestmark = pytest.mark.integration

USAGE_URL = "/api/v1/ai/usage"

PRICED = """
{
  "version": "test-book",
  "prices": [
    {
      "model": "claude-opus-5",
      "input_per_million": "15.00",
      "output_per_million": "75.00",
      "currency": "USD",
      "effective_from": "2020-01-01T00:00:00Z"
    }
  ]
}
"""


async def usage(client: AsyncClient, account, **params: object) -> dict:  # noqa: ANN001
    response = await client.get(USAGE_URL, headers=account.headers(), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def priced_client(session: AsyncSession) -> tuple[FastAPI, AsyncClient]:
    """An application whose deployment has configured prices.

    Shares the test's session, like the shared fixture does, so rows written by
    one are visible to the other.
    """
    application = create_app(Settings(app_env="test", llm_pricing=PRICED))
    application.dependency_overrides[get_session] = lambda: session
    return application, AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    )


# -- What it reports ----------------------------------------------------------


async def test_a_quiet_organization_reports_zeroes_rather_than_failing(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)

    report = await usage(api_client, account)

    assert report["llm_calls"] == 0
    assert report["tokens"]["total_tokens"] == 0
    assert report["agent_runs"]["total"] == 0
    assert report["models"] == []


async def test_an_agent_run_appears_in_the_report(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    report = await usage(api_client, account)

    assert report["agent_runs"]["total"] == 1
    assert report["agent_runs"]["by_status"]["completed"] == 1
    assert report["llm_calls"] == 1


async def test_tokens_are_summed_from_the_model_calls(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The scripted provider reports 11 in and 7 out per call, twice over."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    report = await usage(api_client, account)

    assert report["llm_calls"] == 1
    assert report["tokens"]["input_tokens"] == 11
    assert report["tokens"]["output_tokens"] == 7
    assert report["tokens"]["total_tokens"] == 18


async def test_usage_is_broken_down_by_model(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    report = await usage(api_client, account)

    assert [entry["model"] for entry in report["models"]] == ["claude-opus-5"]
    assert report["models"][0]["calls"] == 1


async def test_tool_executions_are_broken_down_by_tool(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    report = await usage(api_client, account)

    assert [entry["tool_name"] for entry in report["tools"]] == ["cancel_shipment"]
    assert report["tool_executions"]["total"] == 1


async def test_a_workflow_run_and_its_approval_are_counted(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    await start(api_client, account.headers(), workflow_id)

    report = await usage(api_client, account)

    assert report["workflow_runs"]["total"] == 1
    assert report["workflow_runs"]["by_status"]["succeeded"] == 1
    assert report["tool_executions"]["total"] == 2


async def test_an_approval_is_counted_by_how_it_ended(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/reject", headers=account.headers()
    )

    report = await usage(api_client, account)

    assert report["approvals"]["total"] == 1
    assert report["approvals"]["by_status"]["rejected"] == 1


async def test_a_resumed_run_is_not_counted_twice(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """A run that paused for approval and carried on is one run, and its model
    calls are counted once each - the step rows are unique per (run, step)."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(
        f"/api/v1/approvals/{paused['approval']['id']}/approve", headers=account.headers()
    )

    report = await usage(api_client, account)

    assert report["agent_runs"]["total"] == 1, "one run, not one per request"
    assert report["llm_calls"] == 2, "two model calls: the proposal and the answer"
    assert report["tokens"]["total_tokens"] == 36


async def test_the_per_day_breakdown_is_off_unless_asked_for(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    without = await usage(api_client, account)
    with_days = await usage(api_client, account, include_days=True)

    assert without["days"] == []
    assert len(with_days["days"]) == 1
    assert with_days["days"][0]["calls"] == 1


# -- Cost ---------------------------------------------------------------------


async def test_an_unpriced_deployment_reports_usage_and_no_cost(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The default. Complete tokens, and an explicit "we do not know"."""
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    report = await usage(api_client, account)

    assert report["tokens"]["total_tokens"] > 0
    assert report["cost"]["amount"] is None, "unknown, never zero"
    assert report["cost"]["unpriced_calls"] == 1
    assert report["cost"]["unpriced_models"] == ["claude-opus-5"]


async def test_a_priced_deployment_reports_an_exact_decimal(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    _, client = priced_client(session)
    async with client:
        report = await usage(client, account)

    # 11 input at $15/M plus 7 output at $75/M.
    assert report["cost"]["amount"] == "0.000690"
    assert report["cost"]["currency"] == "USD"
    assert report["cost"]["price_version"] == "test-book"
    assert report["cost"]["unpriced_calls"] == 0


async def test_cost_is_a_string_so_no_client_parses_it_as_a_float(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    _, client = priced_client(session)
    async with client:
        response = await client.get(USAGE_URL, headers=account.headers())

    assert isinstance(response.json()["cost"]["amount"], str)
    assert '"amount":"0.000690"' in response.text.replace(" ", "")


# -- Tenant isolation ---------------------------------------------------------


async def test_one_organizations_usage_excludes_anothers(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The assertion an aggregate gets wrong quietly."""
    ours = await register(api_client)
    theirs = await register(api_client)

    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=theirs.headers())

    our_report = await usage(api_client, ours)
    their_report = await usage(api_client, theirs)

    assert our_report["llm_calls"] == 0
    assert our_report["tokens"]["total_tokens"] == 0
    assert our_report["agent_runs"]["total"] == 0
    assert our_report["models"] == []

    assert their_report["llm_calls"] == 1
    assert their_report["tokens"]["total_tokens"] > 0


async def test_the_report_names_the_callers_own_organization(
    api_client: AsyncClient,
) -> None:
    """There is no parameter that names one, and the response proves which."""
    account = await register(api_client)

    report = await usage(api_client, account)

    assert report["organization_id"] == str(account.organization_id)


async def test_a_member_may_read_usage(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Knowing what the organization is spending is not the same as spending it."""
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)

    response = await api_client.get(USAGE_URL, headers=member.headers())

    assert response.status_code == 200


async def test_usage_needs_authentication(api_client: AsyncClient) -> None:
    response = await api_client.get(USAGE_URL)

    assert response.status_code == 401


# -- Bounds -------------------------------------------------------------------


async def test_a_window_longer_than_the_deployment_allows_is_refused(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)
    now = datetime.now(UTC)

    response = await api_client.get(
        USAGE_URL,
        headers=account.headers(),
        params={
            "since": (now - timedelta(days=400)).isoformat(),
            "until": now.isoformat(),
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "usage_window_invalid"


async def test_an_inverted_window_is_refused(api_client: AsyncClient) -> None:
    account = await register(api_client)
    now = datetime.now(UTC)

    response = await api_client.get(
        USAGE_URL,
        headers=account.headers(),
        params={"since": now.isoformat(), "until": (now - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 422


async def test_a_window_that_starts_in_the_future_is_refused(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)
    later = datetime.now(UTC) + timedelta(days=2)

    response = await api_client.get(
        USAGE_URL,
        headers=account.headers(),
        params={"since": later.isoformat(), "until": (later + timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 422


async def test_a_window_excludes_what_happened_before_it(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    now = datetime.now(UTC)
    report = await usage(
        api_client,
        account,
        since=(now - timedelta(days=30)).isoformat(),
        until=(now - timedelta(days=1)).isoformat(),
    )

    assert report["llm_calls"] == 0, "the run is outside this window"


# -- What the report cannot contain -------------------------------------------


async def test_the_report_carries_no_prompt_answer_argument_or_result(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Structural: the report is built from execution records, which never held
    any of these, and no field on the response could carry one."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    text = (await api_client.get(USAGE_URL, headers=account.headers())).text.lower()

    for forbidden in (
        "abc123",
        "the customer asked us to stop it",
        "please cancel",
        "prompt",
        "answer",
        "arguments",
        "idempotency",
        "api_key",
        "secret",
        "authorization",
        "bearer",
        "postgresql://",
    ):
        assert forbidden not in text, f"a usage report must not carry {forbidden!r}"


async def test_the_report_has_no_field_for_raw_execution_data(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)

    report = await usage(api_client, account)

    for forbidden in ("input_data", "output_data", "parameters", "messages", "conversation"):
        assert forbidden not in report


# -- Direct generation in the aggregate ---------------------------------------
#
# Model calls are recorded in two places - `agent_steps` for a call inside a
# run, `generation_usage` for a direct one - and the report adds them. These
# tests are the arithmetic of that addition, and of the thing it must not do.


async def test_a_direct_generation_appears_in_the_report(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)
    provider = _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())

        report = await usage(api_client, account)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert report["llm_calls"] == 1
    assert report["tokens"]["input_tokens"] == provider.input_tokens
    assert report["tokens"]["output_tokens"] == provider.output_tokens


async def test_direct_and_agent_usage_are_added_not_double_counted(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The property the union exists for.

    One agent run (one model call, 11 in / 7 out from the scripted provider)
    plus one direct generation (23 in / 11 out). The report must show two calls
    and the sum - not four calls, and not either half alone.
    """
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)

    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    provider = _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())
        report = await usage(api_client, account)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert report["llm_calls"] == 2
    assert report["tokens"]["input_tokens"] == 11 + provider.input_tokens
    assert report["tokens"]["output_tokens"] == 7 + provider.output_tokens

    # And the agent run is still counted exactly as it was before this table
    # existed: the union added a source, it did not change what a run is.
    assert report["agent_runs"]["total"] == 1
    assert report["agent_runs"]["by_status"]["completed"] == 1


async def test_both_sources_collapse_onto_one_model_row(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Same model, two sources, one row in the breakdown."""
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)

    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())
        report = await usage(api_client, account)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert len(report["models"]) == 1
    assert report["models"][0]["model"] == "claude-opus-5"
    assert report["models"][0]["calls"] == 2


async def test_direct_generation_is_priced_like_any_other_call(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)
    _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    _, client = priced_client(session)
    async with client:
        report = await usage(client, account)

    # 23 input at $15/M plus 11 output at $75/M.
    assert report["cost"]["amount"] == "0.001170"
    assert report["cost"]["currency"] == "USD"


async def test_an_unpriced_model_stays_unpriced_for_direct_generation(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Unknown, never zero - the same rule, on the new source."""
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)
    _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())
        report = await usage(api_client, account)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert report["tokens"]["total_tokens"] > 0
    assert report["cost"]["amount"] is None
    assert report["cost"]["unpriced_calls"] == 1


async def test_one_organizations_direct_generation_is_invisible_to_another(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    ours = await register(api_client)
    theirs = await register(api_client)

    _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=theirs.headers())

        our_report = await usage(api_client, ours)
        their_report = await usage(api_client, theirs)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert our_report["llm_calls"] == 0
    assert our_report["tokens"]["total_tokens"] == 0
    assert their_report["llm_calls"] == 1


async def test_the_per_day_breakdown_covers_both_sources(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    from tests.integration.test_generation_usage import GENERATE, FakeProvider, _install, a_body

    account = await register(api_client)

    script(app, answering("Done."))
    await api_client.post(RUN_URL, json=body(), headers=account.headers())

    _install(app, FakeProvider())
    try:
        await api_client.post(GENERATE, json=a_body(), headers=account.headers())
        report = await usage(api_client, account, include_days=True)
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)

    assert len(report["days"]) == 1
    assert report["days"][0]["calls"] == 2
