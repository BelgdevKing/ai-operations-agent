"""A real request, traced end to end, and what the trace is not allowed to say.

The unit tests prove each filter in isolation. This file runs an actual agent
run - real auth, real tenancy, real runtime, real tool framework, a scripted
provider - with tracing enabled, and then reads every span the request emitted.

Two claims, and the second is the one worth the setup:

1. **The trace has the shape a trace should have.** One trace id across the
   whole request, the HTTP span at the root, the agent run inside it, and the
   model call and the tool call inside that. A flat list of spans is not a
   trace; it is a log with extra fields.
2. **Nothing in it belongs to the tenant.** Not the organization id, not the
   user id, not the run id, not the shipment reference the caller typed, not
   what the model was asked and not what it answered. Asserted against the
   rendered span records rather than against the design, because the design is
   what a regression would still agree with.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.main import create_app
from tests.integration.agent_helpers import (
    ANSWER,
    RUN_URL,
    answering,
    asking,
    body,
    script,
    seed_shipment,
)
from tests.integration.auth_helpers import PASSWORD, Account, register
from tests.integration.workflow_helpers import REFERENCE as WORKFLOW_REFERENCE
from tests.integration.workflow_helpers import lookup_workflow, published
from tests.integration.workflow_helpers import seed as workflow_seed
from tests.integration.workflow_helpers import start as workflow_start

pytestmark = pytest.mark.integration

REFERENCE = "TRACE-REF-9001"
QUESTION = "Where is shipment TRACE-REF-9001?"
ANSWERED = "It is in transit and nothing needs doing."


@contextmanager
def captured_spans() -> Iterator[list[logging.LogRecord]]:
    """Collect the span records this request emits.

    Attached to the tracing logger rather than to the root one: ``create_app``
    calls ``configure_logging``, which replaces the root handlers, so caplog
    does not survive building an application.
    """
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("app.observability.tracing")
    handler = Collector(level=logging.INFO)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture
def traced_app(settings: Settings) -> FastAPI:
    """The same application, with tracing turned on.

    Built rather than mutated: the middleware is handed its tracer when it is
    constructed, so replacing ``app.state.tracer`` afterwards would change
    nothing and the test would pass for the wrong reason.
    """
    return create_app(settings.model_copy(update={"tracing_enabled": True}))


@pytest.fixture
async def traced_client(
    traced_app: FastAPI, session: AsyncSession
) -> AsyncGenerator[AsyncClient, None]:
    """A client on the traced application, sharing the test's transaction."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    traced_app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=traced_app), base_url="http://testserver"
    ) as client:
        yield client

    traced_app.dependency_overrides.clear()


def contexts(records: list[logging.LogRecord]) -> list[dict]:
    return [record.context for record in records]  # type: ignore[attr-defined]


def all_named(records: list[logging.LogRecord], name: str) -> list[dict]:
    """Every span with this name, or a failure saying what was actually there."""
    matches = [context for context in contexts(records) if context["span_name"] == name]
    assert matches, [context["span_name"] for context in contexts(records)]
    return matches


def named(records: list[logging.LogRecord], name: str) -> dict:
    """The one span with this name. Fails if there is more than one."""
    matches = all_named(records, name)
    assert len(matches) == 1, [context["span_name"] for context in contexts(records)]
    return matches[0]


class Traced(NamedTuple):
    """One completed run: the spans it emitted and who ran it."""

    spans: list[logging.LogRecord]
    account: Account


async def run_an_agent(app: FastAPI, client: AsyncClient, session: AsyncSession) -> Traced:
    """One agent run that looks a shipment up, with every span it produced."""
    account = await register(client)
    await seed_shipment(session, account.organization_id, reference=REFERENCE)
    script(app, asking("get_shipment", shipment_reference=REFERENCE), answering(ANSWERED))

    with captured_spans() as spans:
        response = await client.post(RUN_URL, json=body(QUESTION), headers=account.headers())

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    return Traced(spans, account)


# -- The shape of a trace -------------------------------------------------------


async def test_one_request_produces_one_trace(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    spans = (await run_an_agent(traced_app, traced_client, session)).spans

    trace_ids = {context["trace_id"] for context in contexts(spans)}
    assert len(trace_ids) == 1, "the spans of one request must share a trace id"


async def test_the_boundaries_that_matter_are_all_spans(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """The four Part 21 named for an agent request: HTTP in, the run, the model
    call, the tool call. A trace missing the tool call cannot answer the
    question people actually ask, which is where the time went."""
    spans = (await run_an_agent(traced_app, traced_client, session)).spans
    names = {context["span_name"] for context in contexts(spans)}

    assert "agent.run" in names
    assert "llm.generate_structured" in names
    assert "tool.execute" in names
    assert any(name.startswith("POST ") for name in names)


async def test_the_work_nests_under_the_request_that_asked_for_it(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """A flat list of spans is a log. The parent links are the trace."""
    spans = (await run_an_agent(traced_app, traced_client, session)).spans
    by_id = {context["span_id"]: context for context in contexts(spans)}

    server = next(context for context in contexts(spans) if context["span_kind"] == "server")
    run = named(spans, "agent.run")
    tool = named(spans, "tool.execute")

    assert server["parent_span_id"] is None, "nothing upstream, so nothing above it"
    assert run["parent_span_id"] == server["span_id"]
    assert by_id[tool["parent_span_id"]]["span_name"] == "agent.run"


async def test_each_span_says_what_it_was_doing(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    spans = (await run_an_agent(traced_app, traced_client, session)).spans

    run = named(spans, "agent.run")
    assert run["agent.status"] == "completed"
    assert run["agent.tool_call_count"] == 1
    assert run["agent.step_count"] >= 1

    tool = named(spans, "tool.execute")
    assert tool["tool.name"] == "get_shipment"
    assert tool["tool.outcome"] == "succeeded"
    assert tool["tool.safety"] == "read_only"

    # Two model calls: one that decided to use the tool, one that answered
    # once it had the result. Each is its own span, each reports its own usage.
    calls = all_named(spans, "llm.generate_structured")
    assert len(calls) == 2
    for call in calls:
        assert call["llm.model"]
        assert call["llm.input_tokens"] == 11
        assert call["llm.output_tokens"] == 7
        assert call["llm.retries"] == 0


async def test_every_span_names_the_deployment_it_came_from(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    spans = (await run_an_agent(traced_app, traced_client, session)).spans

    for context in contexts(spans):
        assert context["service.name"]
        assert context["deployment.environment.name"] == "test"


# -- What the trace is not allowed to contain -----------------------------------


async def test_no_span_carries_a_tenant_or_an_execution(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """Every identifier in this platform is a UUID, and none of them is here.

    The strongest form of this assertion available: the rendered span records
    are searched for anything UUID-shaped, so it covers the organization, the
    user, the agent, the run, the conversation and the tool execution at once -
    including ones added later.
    """
    import re

    spans = (await run_an_agent(traced_app, traced_client, session)).spans
    uuid_shaped = re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )

    for context in contexts(spans):
        rendered = str(context)
        assert not uuid_shaped.search(rendered), rendered


async def test_no_span_carries_what_was_asked_or_answered(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """The prompt, the model's answer and the tool's arguments are the tenant's
    business record. They live in the conversation and the execution tables,
    both tenant-scoped; a trace store is neither."""
    spans = (await run_an_agent(traced_app, traced_client, session)).spans
    rendered = str(contexts(spans))

    assert QUESTION not in rendered
    assert ANSWERED not in rendered
    assert ANSWER not in rendered
    # The tool's argument, which is also a business reference somebody typed.
    assert REFERENCE not in rendered


async def test_no_span_carries_a_credential_or_a_header(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    spans, account = await run_an_agent(traced_app, traced_client, session)
    rendered = str(contexts(spans))

    # The real ones, from the account that made the request, rather than words
    # that might resemble them: this account's access token and its password.
    assert account.token not in rendered
    assert PASSWORD not in rendered
    assert account.email not in rendered

    lowered = rendered.lower()
    for forbidden in ("authorization", "bearer", "password", "secret", "api_key", "apikey"):
        assert forbidden not in lowered, forbidden


async def test_a_span_carries_only_attributes_from_the_allow_list(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """Belt and braces on the two tests above: rather than listing things that
    must be absent, check that everything present was permitted."""
    from app.observability import names

    structural = {
        "trace_id",
        "span_id",
        "parent_span_id",
        "span_name",
        "span_kind",
        "span_status",
        "duration_ms",
    }

    spans = (await run_an_agent(traced_app, traced_client, session)).spans

    for context in contexts(spans):
        unexpected = set(context) - structural - names.SPAN_ATTRIBUTES
        assert unexpected == set(), unexpected


# -- Off means off ---------------------------------------------------------------


async def test_the_same_request_emits_nothing_with_tracing_off(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The default application, which is what a deployment gets unless it asks.

    Same request, same work, no telemetry - and the response is unchanged,
    which is the half of "safe to disable" that matters to a user.
    """
    with captured_spans() as emitted:
        response_json = (await run_the_same_request(app, api_client, session)).json()

    assert response_json["status"] == "completed"
    assert emitted == []


async def run_the_same_request(app: FastAPI, client: AsyncClient, session: AsyncSession):  # noqa: ANN201
    account = await register(client)
    await seed_shipment(session, account.organization_id, reference=REFERENCE)
    script(app, asking("get_shipment", shipment_reference=REFERENCE), answering(ANSWERED))
    return await client.post(RUN_URL, json=body(QUESTION), headers=account.headers())


# -- The other execution boundaries ----------------------------------------------


async def test_a_workflow_run_traces_its_run_and_each_step(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """A workflow's shape is the question a trace is best at: which step, how
    long, and what the tool underneath it did."""
    account = await register(traced_client)
    await workflow_seed(session, account.organization_id)
    workflow_id = await published(traced_client, account.headers(), lookup_workflow())

    with captured_spans() as spans:
        run = await workflow_start(traced_client, account.headers(), workflow_id)

    assert run["status"] == "succeeded"

    parent = named(spans, "workflow.run")
    assert parent["workflow.status"] == "succeeded"
    assert parent["workflow.step_count"] == 2

    steps = all_named(spans, "workflow.step")
    assert len(steps) == 2
    for step in steps:
        assert step["workflow.step_type"] == "tool_call"
        assert step["workflow.step_status"] == "succeeded"
        assert step["parent_span_id"] == parent["span_id"]

    # The tools the steps called are inside them, not beside them.
    tools = all_named(spans, "tool.execute")
    assert len(tools) == 2
    step_ids = {step["span_id"] for step in steps}
    assert all(tool["parent_span_id"] in step_ids for tool in tools)


async def test_a_workflow_trace_carries_no_step_key_or_payload(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """A step key is a name a workflow author chose, and the input is whatever
    the caller sent. Both are unbounded, and one of them is a business
    reference."""
    account = await register(traced_client)
    await workflow_seed(session, account.organization_id)
    workflow_id = await published(traced_client, account.headers(), lookup_workflow())

    with captured_spans() as spans:
        await workflow_start(traced_client, account.headers(), workflow_id)

    # Checked as values rather than as substrings: `get_shipment_charges` is a
    # registered tool name and legitimately on the `charges` step's child span,
    # so searching the rendered record for "charges" would fail on something
    # that is supposed to be there.
    values = {
        str(value)
        for context in contexts(spans)
        for key, value in context.items()
        if key not in ("trace_id", "span_id", "parent_span_id", "span_name")
    }

    assert "look" not in values
    assert "charges" not in values
    assert WORKFLOW_REFERENCE not in str(contexts(spans))


async def test_deciding_an_approval_traces_the_decision_and_what_it_resumed(
    traced_app: FastAPI, traced_client: AsyncClient, session: AsyncSession
) -> None:
    """The span a person's click produces, and the run that carried on beneath
    it. The decision is the only attribute: which way it went, never who
    decided, which approval it was, or what was approved."""
    account = await register(traced_client)
    await seed_shipment(session, account.organization_id, reference=REFERENCE)
    script(
        traced_app,
        asking("cancel_shipment", shipment_reference=REFERENCE, reason="They asked."),
        answering(ANSWERED),
    )

    paused = await traced_client.post(RUN_URL, json=body(QUESTION), headers=account.headers())
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "awaiting_approval"
    approval_id = paused.json()["approval"]["id"]

    with captured_spans() as spans:
        decided = await traced_client.post(
            f"/api/v1/approvals/{approval_id}/approve", headers=account.headers()
        )

    assert decided.status_code == 200, decided.text

    decision = named(spans, "approval.decide")
    assert decision["approval.decision"] == "approved"

    # The run that was waiting continued underneath it.
    resumed = named(spans, "agent.run")
    assert resumed["parent_span_id"] == decision["span_id"]
    assert resumed["agent.status"] == "completed"

    # And nothing about the action that was approved.
    rendered = str(contexts(spans))
    assert REFERENCE not in rendered
    assert "They asked." not in rendered
