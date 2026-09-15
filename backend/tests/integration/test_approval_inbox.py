"""The approval inbox: what a reviewer can read, and what they cannot.

Part 16 proved that a decision is made once and an execution runs at most once.
This file is about the half-hour before that: somebody opening a queue, working
out what they are being asked, and answering it.

Two properties recur, and they pull in opposite directions, which is what makes
the file worth reading:

**A reviewer must be able to tell what is being proposed.** An approval that
says only "cancel_shipment" cannot be reviewed; it can only be rubber-stamped.
So the queue carries a summary naming the record.

**A reviewer must not be handed the payload.** The summary is not the arguments
with some parts hidden - it is a projection built from what the *tool declared*
in its own code, taken once when the approval was requested. Nothing about the
request, the workflow that called it, or the person reading it can widen that.

The rest is the boundary around the decision itself: a request body that has no
field capable of changing what is being approved, a queue that pages without
skipping, and a tenant wall that answers "no such approval" either way.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import ToolExecutionRecord
from app.models.approval import Approval
from app.models.audit import AuditEvent
from app.models.enums import ApprovalStatus, MemberRole
from tests.integration.auth_helpers import Account, add_member, register
from tests.integration.test_approval_workflow import CANCEL, REASON, pause_a_run

pytestmark = pytest.mark.integration

APPROVALS_URL = "/api/v1/approvals"

# The business values the scripted agent proposes. Both are declared by
# CancelShipmentTool as safe to show an approver; nothing else about the call is.
REFERENCE = "ABC123"


def decide_url(approval_id: str, *, approve: bool) -> str:
    return f"{APPROVALS_URL}/{approval_id}/{'approve' if approve else 'reject'}"


async def queue(client: AsyncClient, account: Account, **params: object) -> dict:
    response = await client.get(APPROVALS_URL, headers=account.headers(), params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def approvals_of(session: AsyncSession, organization_id: uuid.UUID) -> list[Approval]:
    result = await session.execute(
        select(Approval)
        .where(Approval.organization_id == organization_id)
        .order_by(Approval.requested_at, Approval.id)
    )
    return list(result.scalars().all())


async def events_of(
    session: AsyncSession, organization_id: uuid.UUID, event_type: str
) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == organization_id,
            AuditEvent.event_type == event_type,
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
    )
    return list(result.scalars().all())


# -- What is being proposed ---------------------------------------------------


async def test_the_queue_says_what_is_being_done_and_to_what(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    [approval] = (await queue(api_client, account))["approvals"]

    assert approval["summary"] == f"Cancel shipment {REFERENCE}"
    assert approval["tool_name"] == CANCEL


async def test_the_summary_fields_are_the_ones_the_tool_declared(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    [approval] = (await queue(api_client, account))["approvals"]

    assert approval["summary_fields"] == [
        {"label": "Shipment reference", "value": REFERENCE},
        {"label": "Reason", "value": REASON},
    ]


async def test_the_reason_is_the_frameworks_not_the_models(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """A model saying it needs approval is not what gates anything.

    The reason comes from the tool's safety classification, which is written in
    code and cannot be argued with by a generated sentence. The model's own
    words about *why it wants to* appear separately, labelled as an argument it
    supplied - which is the distinction that matters when somebody is deciding
    whether to trust the request.
    """
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    [approval] = (await queue(api_client, account))["approvals"]

    assert "destructive" in approval["reason"]
    assert CANCEL in approval["reason"]
    assert approval["reason"] != REASON


async def test_the_detail_says_what_happens_either_way(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=account.headers()
    )

    detail = response.json()
    assert CANCEL in detail["effect_if_approved"]
    assert "at most once" not in detail["effect_if_approved"], "plain words, not a spec quote"
    assert "never" in detail["effect_if_rejected"]


async def test_the_detail_names_the_run_and_the_execution_it_authorises(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=account.headers()
    )

    detail = response.json()
    assert detail["run_id"] == paused["run_id"]
    assert detail["tool_execution_id"] is not None
    assert detail["organization_id"] == str(account.organization_id)
    assert detail["expires_at"] is not None, "every approval is given a deadline"


# -- What is not --------------------------------------------------------------


async def test_the_detail_has_no_field_for_the_arguments(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Structural: not hidden, not redacted - absent from the contract."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=account.headers()
    )

    detail = response.json()
    for forbidden in ("parameters", "arguments", "tool_args", "payload", "input", "result"):
        assert forbidden not in detail


async def test_nothing_in_the_queue_names_a_provider_a_key_or_a_prompt(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    body = (await api_client.get(APPROVALS_URL, headers=account.headers())).text.lower()

    for forbidden in (
        "api_key",
        "apikey",
        "secret",
        "authorization",
        "bearer",
        "postgresql://",
        "anthropic",
        "openai",
        "system_prompt",
        "reasoning",
    ):
        assert forbidden not in body, f"the queue must not carry {forbidden}"


async def test_the_parameters_column_is_still_empty(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The durable-execution phase's rule, unchanged by anything here."""
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    [approval] = await approvals_of(session, account.organization_id)

    assert approval.parameters == {}


# -- Reading the queue --------------------------------------------------------


async def test_the_queue_shows_what_is_waiting_by_default(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(
        decide_url(paused["approval"]["id"], approve=False), headers=account.headers()
    )

    page = await queue(api_client, account)

    assert page["approvals"] == [], "a decided approval is not waiting on anybody"


async def test_a_status_filter_finds_what_was_decided(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(
        decide_url(paused["approval"]["id"], approve=False), headers=account.headers()
    )

    page = await queue(api_client, account, status="rejected")

    assert [item["status"] for item in page["approvals"]] == ["rejected"]


async def test_an_unknown_status_is_refused_rather_than_ignored(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)

    response = await api_client.get(
        APPROVALS_URL, headers=account.headers(), params={"status": "probably"}
    )

    assert response.status_code == 422


async def test_a_tool_filter_matches_exactly(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    matching = await queue(api_client, account, tool_name=CANCEL)
    other = await queue(api_client, account, tool_name="get_shipment")
    partial = await queue(api_client, account, tool_name="cancel")

    assert len(matching["approvals"]) == 1
    assert other["approvals"] == []
    assert partial["approvals"] == [], "a name is an identifier, not a pattern"


async def test_the_page_is_bounded_whatever_is_asked_for(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.get(
        APPROVALS_URL, headers=account.headers(), params={"limit": 10_000}
    )

    assert response.status_code == 422, "the bound is a contract, not a silent clamp"


async def test_a_short_page_is_the_last_one(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    page = await queue(api_client, account, limit=50)

    assert len(page["approvals"]) == 1
    assert page["next_cursor"] is None


async def test_a_full_page_carries_a_cursor_and_the_next_page_continues(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Two organizations' worth of work would be simpler; one run is what we have.

    With a single approval and a page size of one, the first page is "full" and
    therefore has to offer a cursor - because whether anything follows it cannot
    be known without asking. The second page answers that it does not.
    """
    account = await register(api_client)
    await pause_a_run(app, api_client, session, account)

    first = await queue(api_client, account, limit=1)
    assert len(first["approvals"]) == 1
    assert first["next_cursor"] is not None

    second = await queue(api_client, account, limit=1, cursor=first["next_cursor"])
    assert second["approvals"] == []
    assert second["next_cursor"] is None


async def test_a_cursor_from_nowhere_is_refused(api_client: AsyncClient) -> None:
    account = await register(api_client)

    response = await api_client.get(
        APPROVALS_URL, headers=account.headers(), params={"cursor": str(uuid.uuid4())}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "approval_invalid_cursor"


async def test_another_organizations_cursor_is_refused_the_same_way(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Paging must not become a way of asking whether somebody else's approval
    is real. An id from another tenant and an id from nowhere get one answer."""
    ours = await register(api_client)
    theirs = await register(api_client)
    paused = await pause_a_run(app, api_client, session, theirs)

    response = await api_client.get(
        APPROVALS_URL,
        headers=ours.headers(),
        params={"cursor": paused["approval"]["id"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "approval_invalid_cursor"


# -- Who may do what ----------------------------------------------------------


async def test_a_member_may_read_the_detail(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)
    paused = await pause_a_run(app, api_client, session, owner)

    response = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=member.headers()
    )

    assert response.status_code == 200
    assert response.json()["summary"] == f"Cancel shipment {REFERENCE}"


async def test_a_member_cannot_reject_either(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Refusing is a decision too, and the same role decides both ways."""
    owner = await register(api_client)
    member = await add_member(api_client, session, owner.organization_id, MemberRole.MEMBER)
    paused = await pause_a_run(app, api_client, session, owner)

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=False), headers=member.headers()
    )

    assert response.status_code == 403
    [approval] = await approvals_of(session, owner.organization_id)
    assert approval.status is ApprovalStatus.PENDING


async def test_another_organization_cannot_read_the_detail(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    paused = await pause_a_run(app, api_client, session, ours)

    response = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=theirs.headers()
    )

    assert response.status_code == 404, "404, so guessing an id confirms nothing"
    assert response.json()["error"]["code"] == "approval_not_found"


# -- The decision body --------------------------------------------------------


async def test_a_decision_may_carry_a_reason(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=False),
        json={"reason": "The shipment should stay active."},
        headers=account.headers(),
    )

    assert response.status_code == 200
    assert response.json()["approval"]["decision_reason"] == "The shipment should stay active."


async def test_a_decision_needs_no_reason(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=False), headers=account.headers()
    )

    assert response.status_code == 200
    assert response.json()["approval"]["decision_reason"] is None


async def test_an_oversized_reason_is_refused(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    response = await api_client.post(
        decide_url(paused["approval"]["id"], approve=False),
        json={"reason": "x" * 5_000},
        headers=account.headers(),
    )

    assert response.status_code == 422
    [approval] = await approvals_of(session, account.organization_id)
    assert approval.status is ApprovalStatus.PENDING, "a refused body decides nothing"


async def test_a_hostile_reason_is_stored_as_plain_text(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Control characters go; markup stays, as text, to be escaped where it is
    rendered. Storing an HTML entity would only move the decoding problem."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    await api_client.post(
        decide_url(paused["approval"]["id"], approve=False),
        json={"reason": "<script>alert(1)</script>\x00\x1b[2J\n\n\ndone"},
        headers=account.headers(),
    )

    [approval] = await approvals_of(session, account.organization_id)
    assert approval.decision_reason is not None
    assert "\x00" not in approval.decision_reason
    assert "\x1b" not in approval.decision_reason
    assert approval.decision_reason == "<script>alert(1)</script> [2J done"


async def test_the_body_cannot_name_the_action(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """The immutability property, asserted where it is actually enforced.

    Fetch an approval, alter what it proposed, submit it - the request is
    refused at the edge rather than having the extra fields ignored, so the
    guarantee does not depend on every handler behind this one remembering not
    to read them.
    """
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    for tampered in (
        {"tool_name": "get_shipment"},
        {"tool_execution_id": str(uuid.uuid4())},
        {"run_id": str(uuid.uuid4())},
        {"organization_id": str(uuid.uuid4())},
        {"arguments": {"shipment_reference": "XYZ999"}},
        {"reason": "fine", "shipment_reference": "XYZ999"},
    ):
        response = await api_client.post(
            decide_url(paused["approval"]["id"], approve=True),
            json=tampered,
            headers=account.headers(),
        )
        assert response.status_code == 422, f"{tampered} must be refused"

    [approval] = await approvals_of(session, account.organization_id)
    assert approval.status is ApprovalStatus.PENDING
    assert approval.tool_name == CANCEL
    assert approval.summary == f"Cancel shipment {REFERENCE}"


async def test_the_approved_execution_is_the_one_that_was_proposed(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """Binding, end to end: the id in the queue is the id that ran."""
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    # The id as the reviewer sees it, from the queue they are deciding from.
    detail = await api_client.get(
        f"{APPROVALS_URL}/{paused['approval']['id']}", headers=account.headers()
    )
    proposed = detail.json()["tool_execution_id"]

    decided = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert decided.json()["approval"]["tool_execution_id"] == proposed

    # And at the database, which is where "the same execution" actually means
    # something: one row, the proposed id, marked as having run.
    executions = (
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [str(execution.id) for execution in executions] == [proposed]
    assert executions[0].executed is True


async def test_a_repeated_decision_is_a_conflict_and_runs_nothing_twice(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    first = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )
    second = await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "approval_already_decided"


# -- The trail ----------------------------------------------------------------


async def test_a_decision_is_recorded_with_who_and_which_way(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)

    await api_client.post(
        decide_url(paused["approval"]["id"], approve=False),
        json={"reason": "Leave it running."},
        headers=account.headers(),
    )

    [event] = await events_of(session, account.organization_id, "approval.rejected")
    assert event.action == "rejected"
    assert event.user_id == account.user_id
    assert event.event_metadata["tool_name"] == CANCEL


async def test_the_trail_records_that_a_reason_existed_not_what_it_said(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    """An audit row is read by everyone who can see the organization's history.

    Whether a reason was given, and how long it was, is what an auditor needs
    from there; the sentence itself is one row away, with the approval it is
    about.
    """
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    written = "Because the customer telephoned about shipment ABC123."

    await api_client.post(
        decide_url(paused["approval"]["id"], approve=False),
        json={"reason": written},
        headers=account.headers(),
    )

    [event] = await events_of(session, account.organization_id, "approval.rejected")
    assert event.event_metadata["reason_given"] is True
    assert event.event_metadata["reason_length"] == len(written)
    assert written not in str(event.event_metadata)


async def test_no_audit_row_carries_the_business_values(
    app: FastAPI, api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    paused = await pause_a_run(app, api_client, session, account)
    await api_client.post(
        decide_url(paused["approval"]["id"], approve=True), headers=account.headers()
    )

    rows = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.organization_id == account.organization_id)
            )
        )
        .scalars()
        .all()
    )
    written = str([row.event_metadata for row in rows])

    assert REFERENCE not in written, "the summary is for reviewers, not for the trail"
    assert REASON not in written
    assert "idempotency_key" not in written


# -- A deployment that cannot generate ----------------------------------------
#
# These tests deliberately do NOT script a provider. The test settings carry no
# credential, so the real gateway raises the moment it is built - which makes
# "was a gateway built?" observable from the status code.
#
# The human-in-the-loop surface must work without one. Reading a queue, reading
# a run and stopping a paused run never call a model, and a 500 about a provider
# would be both wrong and actively misleading to whoever is trying to stop
# something.


async def test_the_queue_is_readable_without_a_provider_credential(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)

    response = await api_client.get(APPROVALS_URL, headers=account.headers())

    assert response.status_code == 200
    assert response.json()["approvals"] == []


async def test_runs_are_readable_without_a_provider_credential(
    api_client: AsyncClient,
) -> None:
    account = await register(api_client)

    listed = await api_client.get("/api/v1/ai/runs", headers=account.headers())
    missing = await api_client.get(f"/api/v1/ai/runs/{uuid.uuid4()}", headers=account.headers())

    assert listed.status_code == 200
    assert missing.status_code == 404, "not found, not a provider error"


async def test_a_run_can_be_stopped_without_a_provider_credential(
    api_client: AsyncClient,
) -> None:
    """The one that matters most: somebody trying to stop something in a hurry.

    A deployment whose provider credential has been revoked or rotated is
    exactly when a paused run most needs stopping, and it is the worst possible
    moment to answer "the language model provider is not configured correctly".
    """
    account = await register(api_client)

    response = await api_client.post(
        f"/api/v1/ai/runs/{uuid.uuid4()}/cancel", headers=account.headers()
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_run_not_found"
