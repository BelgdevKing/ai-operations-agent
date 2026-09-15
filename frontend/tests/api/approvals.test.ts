/**
 * The approval endpoints as the workspace uses them, and what the interface is
 * allowed to know about a pending action.
 *
 * The recurring assertion is a negative one: nothing the backend sends carries
 * a tool's arguments, so there is nothing for a screen to disclose. That is a
 * property of the contract, not of the rendering - which is why it is asserted
 * against the payload rather than against a component.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  acme,
  agentRunResponse,
  approvalResponse,
  awaitingApproval,
  CONVERSATION_ID,
  FAKE_TOKEN,
  RUN_ID,
  storedConversation,
} from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import {
  approveAction,
  ApiError,
  createAuthenticatedApi,
  describeError,
  getConversation,
  getRun,
  listApprovals,
  listConversations,
  rejectAction,
} from "@/lib/api";
import { isAdministrator } from "@/lib/auth/permissions";
import {
  conversationReducer as reduce,
  EMPTY_CONVERSATION,
  isAwaitingApproval,
} from "@/lib/ai/conversation";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

const APPROVAL_ID = "77777777-7777-4777-8777-777777777777";

/** Source with block and line comments removed, for scanning actual code. */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

// -- The queue ----------------------------------------------------------------

test("the queue is read from the approvals resource", async () => {
  const fetch = stubFetch(() => jsonResponse([approvalResponse()]));
  try {
    await listApprovals(caller);

    assert.equal(fetch.last.method, "GET");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/approvals");
  } finally {
    fetch.restore();
  }
});

test("the queue is authenticated and names the acting organization", async () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    await listApprovals(caller);

    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("a queued approval says which action and why, and not what it was asked", async () => {
  const fetch = stubFetch(() => jsonResponse([approvalResponse()]));
  try {
    const [approval] = await listApprovals(caller);

    assert.equal(approval?.tool_name, "cancel_shipment");
    assert.match(approval?.reason ?? "", /destructive/);
    // Structural: there is no field for the arguments, in the type or the body.
    assert.equal("parameters" in (approval ?? {}), false);
    assert.equal("arguments" in (approval ?? {}), false);
  } finally {
    fetch.restore();
  }
});

// -- Deciding -----------------------------------------------------------------

test("approving posts to the approve path", async () => {
  const fetch = stubFetch(() => jsonResponse(agentRunResponse("Cancelled.")));
  try {
    await approveAction(caller, APPROVAL_ID);

    assert.equal(fetch.last.method, "POST");
    assert.equal(
      new URL(fetch.last.url).pathname,
      `/api/v1/approvals/${APPROVAL_ID}/approve`,
    );
  } finally {
    fetch.restore();
  }
});

test("rejecting posts to a different path, so the two cannot be confused", async () => {
  // Which decision was made is in the request line rather than a body field: a
  // client cannot get it wrong, and a replayed request cannot turn a refusal
  // into an approval.
  const fetch = stubFetch(() => jsonResponse(agentRunResponse("Not cancelled.")));
  try {
    await rejectAction(caller, APPROVAL_ID);

    assert.equal(new URL(fetch.last.url).pathname, `/api/v1/approvals/${APPROVAL_ID}/reject`);
    assert.equal(fetch.last.body, undefined, "no body to disagree with the path");
  } finally {
    fetch.restore();
  }
});

test("a decision says which process it resumed", async () => {
  // Two kinds of process pause on an approval, so the response names the one it
  // carried forward rather than leaving a client to infer it from the shape.
  const fetch = stubFetch(() =>
    jsonResponse({
      approval: approvalResponse({ status: "approved" }),
      agent_run: agentRunResponse("Cancelled.", ["cancel_shipment"]),
      workflow_run: null,
    }),
  );
  try {
    const decision = await approveAction(caller, APPROVAL_ID);

    assert.equal(decision.approval.status, "approved");
    assert.equal(decision.workflow_run, null);
    assert.equal(decision.agent_run?.run_id, RUN_ID);
    assert.equal(decision.agent_run?.status, "completed");
    assert.deepEqual(
      decision.agent_run?.tool_calls.map((call) => call.tool_name),
      ["cancel_shipment"],
    );
  } finally {
    fetch.restore();
  }
});

test("a member who tries anyway is refused by the backend", async () => {
  // The button is hidden from them; this is what happens if they post regardless.
  // The frontend's copy of the rule is a courtesy, and this is the boundary.
  const fetch = stubFetch(() =>
    errorResponse(403, "permission_denied", "This action requires the admin role or higher."),
  );
  try {
    await assert.rejects(
      () => approveAction(caller, APPROVAL_ID),
      (error: unknown) => {
        assert.equal(error instanceof ApiError && error.status, 403);
        assert.equal(describeError(error).title, "Access denied");
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("a second decision is reported as a conflict, not a second action", async () => {
  const fetch = stubFetch(() =>
    errorResponse(409, "approval_already_decided", "That approval has already been decided."),
  );
  try {
    await assert.rejects(
      () => approveAction(caller, APPROVAL_ID),
      (error: unknown) => error instanceof ApiError && error.status === 409,
    );
  } finally {
    fetch.restore();
  }
});

test("only administrators are offered the controls", () => {
  // Mirrors the backend's require_role(ADMIN). Nothing is granted on this.
  assert.equal(isAdministrator("owner"), true);
  assert.equal(isAdministrator("admin"), true);
  assert.equal(isAdministrator("member"), false);
});

// -- Coming back to a paused conversation -------------------------------------

test("a reopened page finds the stored conversation", async () => {
  const fetch = stubFetch((request) =>
    request.url.includes(CONVERSATION_ID)
      ? jsonResponse(storedConversation())
      : jsonResponse([{ id: CONVERSATION_ID, title: "x", created_at: "", updated_at: "" }]),
  );
  try {
    const [summary] = await listConversations(caller);
    const detail = await getConversation(caller, summary!.id);

    assert.equal(detail.id, CONVERSATION_ID);
    assert.equal(detail.turns.length, 4);
  } finally {
    fetch.restore();
  }
});

test("a stored tool turn carries a name and no content to leak", async () => {
  const fetch = stubFetch(() => jsonResponse(storedConversation()));
  try {
    const detail = await getConversation(caller, CONVERSATION_ID);
    const toolTurns = detail.turns.filter((turn) => turn.role.startsWith("tool_"));

    assert.equal(toolTurns.length, 2);
    for (const turn of toolTurns) {
      assert.equal(turn.content, null, "the arguments and the records are not sent");
      assert.equal(turn.tool_name, "get_shipment");
    }
  } finally {
    fetch.restore();
  }
});

test("a run read back while somebody is deciding is still paused", async () => {
  const fetch = stubFetch(() => jsonResponse(awaitingApproval()));
  try {
    const run = await getRun(caller, RUN_ID);
    const state = reduce(EMPTY_CONVERSATION, {
      type: "loaded",
      conversation: storedConversation(),
      run,
    });

    assert.equal(run.status, "awaiting_approval");
    assert.equal(run.final_response, null, "nothing has been answered");
    assert.equal(isAwaitingApproval(state), true);
    assert.equal(state.approval?.id, run.approval?.id);
  } finally {
    fetch.restore();
  }
});

test("the durable workspace reaches for no browser storage", () => {
  // The whole durability story is server-side, and deliberately so: a
  // transcript or a token in localStorage would be readable by any script that
  // got onto the page, would survive a sign-out, and would outlive the tenant
  // switch that is supposed to change what is on screen.
  //
  // Read from source rather than exercised, because the guarantee is "there is
  // no such call" - and the only way to test the absence of a call is to look.
  const root = new URL("../../src/", import.meta.url);
  const files = [
    "lib/ai/conversation.ts",
    "components/ai/ai-workspace.tsx",
    "components/ai/approval-panel.tsx",
    "components/ai/conversation-view.tsx",
    "lib/api/endpoints.ts",
  ];

  for (const file of files) {
    // Comments are stripped first: these modules explain *why* they use no
    // storage, and a check that failed on the explanation would be the kind of
    // test people delete rather than fix.
    const source = withoutComments(readFileSync(new URL(file, root), "utf8"));

    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not use ${forbidden}`);
    }
  }
});
