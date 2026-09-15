/**
 * The approval inbox as the console uses it.
 *
 * Two kinds of assertion, and the second kind is the point of the file.
 *
 * **What the screen may show.** The queue carries a summary and labelled fields,
 * because a reviewer who cannot see which record is affected cannot review
 * anything - they can only rubber-stamp.
 *
 * **What the screen may send.** A decision, and at most a reason. There is no
 * call in this client that can carry a tool name, an execution id, a run id or
 * an argument payload with a decision, so "fetch an approval, alter it, submit
 * it" is not a request this frontend is able to construct.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  acme,
  approvalQueue,
  approvalResponse,
  agentRunResponse,
  FAKE_TOKEN,
} from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import {
  ApiError,
  approveAction,
  cancelRun,
  createAuthenticatedApi,
  describeError,
  getApproval,
  listApprovals,
  rejectAction,
} from "@/lib/api";
import { describeRemaining, remainingMs } from "@/lib/approvals/expiry";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

const APPROVAL_ID = "77777777-7777-4777-8777-777777777777";
const RUN_ID = "33333333-3333-4333-8333-333333333333";

// -- Reading the queue --------------------------------------------------------

test("the inbox is a page, not a bare list", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalQueue()));
  try {
    const page = await listApprovals(caller);

    assert.equal(page.approvals.length, 1);
    assert.equal(page.next_cursor, null);
  } finally {
    fetch.restore();
  }
});

test("asking for nothing in particular asks for no filters", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalQueue()));
  try {
    await listApprovals(caller);

    assert.equal(new URL(fetch.last.url).search, "", "the backend's default is what is waiting");
  } finally {
    fetch.restore();
  }
});

test("a status filter is repeated rather than joined", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalQueue([])));
  try {
    await listApprovals(caller, { status: ["approved", "rejected"] });

    const query = new URL(fetch.last.url).searchParams;
    assert.deepEqual(query.getAll("status"), ["approved", "rejected"]);
  } finally {
    fetch.restore();
  }
});

test("paging asks from the last row seen", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalQueue([])));
  try {
    await listApprovals(caller, { cursor: APPROVAL_ID, limit: 25 });

    const query = new URL(fetch.last.url).searchParams;
    assert.equal(query.get("cursor"), APPROVAL_ID);
    assert.equal(query.get("limit"), "25");
    assert.equal(query.has("offset"), false, "an offset would skip newly queued items");
  } finally {
    fetch.restore();
  }
});

test("a tool filter is sent as written", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalQueue([])));
  try {
    await listApprovals(caller, { toolName: "cancel_shipment" });

    assert.equal(new URL(fetch.last.url).searchParams.get("tool_name"), "cancel_shipment");
  } finally {
    fetch.restore();
  }
});

test("an unusable cursor is reported as a check-the-details failure", async () => {
  const fetch = stubFetch(() =>
    errorResponse(422, "approval_invalid_cursor", "That page cursor is not valid here."),
  );
  try {
    await assert.rejects(
      () => listApprovals(caller, { cursor: "not-a-page" }),
      (error: unknown) => {
        assert.match(describeError(error).title, /Check the details/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

// -- What one approval says ---------------------------------------------------

test("an approval says what is being done, on what, and why", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalResponse()));
  try {
    const approval = await getApproval(caller, APPROVAL_ID);

    assert.equal(approval.summary, "Cancel shipment ABC123");
    assert.deepEqual(
      approval.summary_fields.map((field) => field.label),
      ["Shipment reference", "Reason"],
    );
    assert.match(approval.reason ?? "", /destructive/);
  } finally {
    fetch.restore();
  }
});

test("an approval says what happens either way", async () => {
  const fetch = stubFetch(() => jsonResponse(approvalResponse()));
  try {
    const approval = await getApproval(caller, APPROVAL_ID);

    assert.match(approval.effect_if_approved, /runs once/);
    assert.match(approval.effect_if_rejected, /never/);
  } finally {
    fetch.restore();
  }
});

test("an approval carries no payload to render", async () => {
  // Structural: there is no field for it, in the type or the body.
  const fetch = stubFetch(() => jsonResponse(approvalResponse()));
  try {
    const approval = await getApproval(caller, APPROVAL_ID);

    for (const forbidden of ["parameters", "arguments", "tool_args", "payload"]) {
      assert.equal(forbidden in approval, false, `an approval must not carry ${forbidden}`);
    }
  } finally {
    fetch.restore();
  }
});

// -- Deciding -----------------------------------------------------------------

test("a decision with a reason sends exactly one field", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({ approval: approvalResponse(), agent_run: null, workflow_run: null }),
  );
  try {
    await rejectAction(caller, APPROVAL_ID, { reason: "Leave it running." });

    assert.deepEqual(fetch.last.body, { reason: "Leave it running." });
  } finally {
    fetch.restore();
  }
});

test("a decision without a reason sends no body at all", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({ approval: approvalResponse(), agent_run: null, workflow_run: null }),
  );
  try {
    await approveAction(caller, APPROVAL_ID);

    assert.equal(fetch.last.body, undefined);
  } finally {
    fetch.restore();
  }
});

test("a blank reason is the same as none", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({ approval: approvalResponse(), agent_run: null, workflow_run: null }),
  );
  try {
    await approveAction(caller, APPROVAL_ID, { reason: "   " });

    assert.equal(fetch.last.body, undefined, "one representation of 'they said nothing'");
  } finally {
    fetch.restore();
  }
});

test("nothing in a decision request names the action", async () => {
  // The immutability property as this client can express it: there is no call
  // shape here that carries what is being approved.
  const fetch = stubFetch(() =>
    jsonResponse({ approval: approvalResponse(), agent_run: null, workflow_run: null }),
  );
  try {
    await approveAction(caller, APPROVAL_ID, { reason: "Fine by me." });

    const sent = JSON.stringify(fetch.last.body);
    for (const forbidden of [
      "tool_name",
      "tool_execution_id",
      "run_id",
      "organization_id",
      "arguments",
    ]) {
      assert.equal(sent.includes(forbidden), false, `"${forbidden}" must not be in the body`);
    }
  } finally {
    fetch.restore();
  }
});

test("an expired approval is reported distinctly from a conflict", async () => {
  const fetch = stubFetch(() =>
    errorResponse(410, "approval_expired", "That approval expired before it was decided."),
  );
  try {
    await assert.rejects(
      () => approveAction(caller, APPROVAL_ID),
      (error: unknown) => error instanceof ApiError && error.status === 410,
    );
  } finally {
    fetch.restore();
  }
});

test("an over-long reason is refused by the backend", async () => {
  const fetch = stubFetch(() =>
    errorResponse(422, "validation_error", "The request body is invalid."),
  );
  try {
    await assert.rejects(
      () => rejectAction(caller, APPROVAL_ID, { reason: "x".repeat(5000) }),
      (error: unknown) => error instanceof ApiError && error.status === 422,
    );
  } finally {
    fetch.restore();
  }
});

// -- Stopping a run -----------------------------------------------------------

test("stopping a run posts to its own path and sends nothing", async () => {
  const fetch = stubFetch(() => jsonResponse(agentRunResponse("")));
  try {
    await cancelRun(caller, RUN_ID);

    assert.equal(fetch.last.method, "POST");
    assert.equal(new URL(fetch.last.url).pathname, `/api/v1/ai/runs/${RUN_ID}/cancel`);
    assert.equal(fetch.last.body, undefined);
  } finally {
    fetch.restore();
  }
});

test("a run that is not waiting cannot be stopped, and says so", async () => {
  const fetch = stubFetch(() =>
    errorResponse(
      409,
      "agent_run_not_cancellable",
      "That run is not waiting for approval, so it cannot be cancelled.",
    ),
  );
  try {
    await assert.rejects(
      () => cancelRun(caller, RUN_ID),
      (error: unknown) => error instanceof ApiError && error.status === 409,
    );
  } finally {
    fetch.restore();
  }
});

// -- The countdown ------------------------------------------------------------

test("an absent or unreadable deadline is simply not shown", () => {
  assert.equal(remainingMs(null), null);
  assert.equal(remainingMs("not a date"), null);
});

test("a deadline is described in the largest useful unit", () => {
  assert.equal(describeRemaining(30_000), "under a minute");
  assert.equal(describeRemaining(60_000), "1 minute");
  assert.equal(describeRemaining(5 * 60_000), "5 minutes");
  assert.equal(describeRemaining(3 * 3_600_000), "3 hours");
  assert.equal(describeRemaining(50 * 3_600_000), "2 days");
});

// -- Storage ------------------------------------------------------------------

test("the approval inbox reaches for no browser storage", () => {
  // Read from source rather than exercised, because the guarantee is "there is
  // no such call" - and the only way to test the absence of a call is to look.
  // An approval queue in localStorage would be a list of this tenant's pending
  // business actions, readable by any script on the page and surviving a
  // sign-out.
  const root = new URL("../../src/", import.meta.url);

  for (const file of [
    "components/approvals/approval-inbox.tsx",
    "app/(app)/approvals/page.tsx",
    "components/ai/approval-panel.tsx",
  ]) {
    const source = readFileSync(new URL(file, root), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/.*$/gm, "$1");

    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not use ${forbidden}`);
    }
  }
});

test("the approval screens render text, never markup", () => {
  // No dangerouslySetInnerHTML anywhere near a summary, a reason or a decision
  // reason. Every one of those is text somebody else wrote - a model, or a
  // person typing into a box - and JSX escaping is the whole mitigation.
  const root = new URL("../../src/", import.meta.url);

  for (const file of [
    "components/approvals/approval-inbox.tsx",
    "components/ai/approval-panel.tsx",
  ]) {
    const source = readFileSync(new URL(file, root), "utf8");

    assert.equal(source.includes("dangerouslySetInnerHTML"), false, `${file} must not inject HTML`);
    assert.equal(source.includes("innerHTML"), false, `${file} must not set innerHTML`);
  }
});
