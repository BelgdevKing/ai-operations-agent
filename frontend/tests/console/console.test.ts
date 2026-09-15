/**
 * The agent console: what it may show, and what it must not.
 *
 * This project's test runner has no DOM and no test dependencies, so component
 * behaviour is asserted the way the rest of the suite asserts it: over the
 * *source*, for properties that are about the absence of something. "There is
 * no call to localStorage" and "there is no prop for tool arguments" are
 * statements you can only check by looking, and they are exactly the statements
 * that matter here.
 *
 * The console's API behaviour - runs, retries, approvals, errors - is exercised
 * against the fetch stub in `tests/api/`, which is where it already lived
 * before this part and where it stays.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  acme,
  agentRunResponse,
  awaitingApproval,
  FAKE_TOKEN,
  storedConversation,
} from "../support/fixtures";
import { jsonResponse, stubFetch } from "../support/fetch-stub";
import { createAuthenticatedApi, listAgents, listConversations } from "@/lib/api";
import {
  conversationReducer as reduce,
  EMPTY_CONVERSATION,
  isAwaitingApproval,
} from "@/lib/ai/conversation";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

const ROOT = new URL("../../src/", import.meta.url);

/** Every file the console is built from, including the ones it composes. */
const CONSOLE_SOURCES = [
  "components/ai/ai-workspace.tsx",
  "components/ai/conversation-view.tsx",
  "components/ai/approval-panel.tsx",
  "components/ai/message-composer.tsx",
  "components/console/agent-picker.tsx",
  "components/console/conversation-picker.tsx",
  "components/console/execution-panel.tsx",
  "components/console/tool-activity.tsx",
  "app/(app)/ai/page.tsx",
] as const;

function read(file: string): string {
  return readFileSync(new URL(file, ROOT), "utf8");
}

/** Source with block and line comments removed, for scanning actual code. */
function code(file: string): string {
  return read(file)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

// -- Browser storage ----------------------------------------------------------

test("no part of the console reaches for browser storage", () => {
  // The whole durability story is server-side, deliberately: a transcript, an
  // approval or a token in localStorage would be readable by any script that
  // got onto the page, would survive a sign-out, and would outlive the tenant
  // switch that is supposed to change what is on screen.
  for (const file of CONSOLE_SOURCES) {
    const source = code(file);

    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not use ${forbidden}`);
    }
  }
});

// -- Markup and payloads ------------------------------------------------------

test("the console renders text, never markup", () => {
  // Every string on screen came from a model, a tool declaration or a person
  // typing into a box. JSX escaping is the whole mitigation, and injecting HTML
  // anywhere here would discard it.
  for (const file of CONSOLE_SOURCES) {
    const source = read(file);

    assert.equal(source.includes("dangerouslySetInnerHTML"), false, file);
    assert.equal(source.includes("innerHTML"), false, file);
  }
});

test("tool activity has no way to render arguments or results", () => {
  // Structural. `ToolCallSummary` is two fields, and this component reads both
  // of them; there is no prop, no destructuring and no property access that
  // could reach a payload, because the API never sends one.
  const source = code("components/console/tool-activity.tsx");

  for (const forbidden of [
    "arguments",
    "parameters",
    "payload",
    ".data",
    ".result",
    "input_data",
    "output_data",
  ]) {
    assert.equal(source.includes(forbidden), false, `tool activity must not read ${forbidden}`);
  }
});

test("the execution panel shows figures, not identifiers", () => {
  // Run, conversation and execution ids are correlation handles for a log, not
  // information an operator acts on. Printing one invites somebody to paste it
  // into a URL as though it were an authorization.
  const source = code("components/console/execution-panel.tsx");

  for (const forbidden of ["run.run_id", "run.agent_id", "run.conversation_id"]) {
    assert.equal(source.includes(forbidden), false, `must not render ${forbidden}`);
  }
});

// -- Tenancy ------------------------------------------------------------------

test("nothing in the console lets a user choose a tenant", () => {
  // Which organization a request acts on comes from the session and is checked
  // again by the backend on every call. A control for it here would be a
  // control over somebody else's data, or - more likely - a lie.
  for (const file of CONSOLE_SOURCES) {
    const source = code(file);

    for (const forbidden of ["organization_id", "tenant_id", "organizationId=", "tenantId"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not set ${forbidden}`);
    }
  }
});

test("the console reads the acting organization rather than choosing it", () => {
  const source = code("components/ai/ai-workspace.tsx");

  // It is used as a *key*, so switching tenant remounts and starts from that
  // organization's own agents - never as a parameter that selects one.
  assert.ok(source.includes("active.organization.id"));
  assert.ok(source.includes("useAuthenticatedSession"));
});

// -- Authorization ------------------------------------------------------------

test("the approval controls are a courtesy, and the source says so", () => {
  const source = read("components/ai/approval-panel.tsx");

  assert.match(source, /not a security boundary|boundary/i);
  // `canDecide` hides a control somebody could not use. It does not grant one:
  // a member who posts to the approve path anyway is refused by the backend.
  assert.ok(source.includes("canDecide"));
});

test("the console asks the session for the role and never assumes it", () => {
  const source = code("components/ai/ai-workspace.tsx");

  assert.ok(source.includes("isAdministrator(active.role)"));
  assert.equal(source.includes("canDecide={true}"), false);
});

// -- Accessibility ------------------------------------------------------------

test("the agent picker is a radio group, so arrow keys work without code", () => {
  const source = read("components/console/agent-picker.tsx");

  assert.ok(source.includes('role="radiogroup"'));
  assert.ok(source.includes('role="radio"'));
  assert.ok(source.includes("aria-checked"));
  assert.ok(source.includes('aria-label="Agent"'));
});

test("every console region is labelled", () => {
  const labelled: Array<[string, string]> = [
    ["components/console/execution-panel.tsx", 'aria-label="Execution"'],
    ["components/console/tool-activity.tsx", 'aria-label="Tool activity"'],
    ["components/console/conversation-picker.tsx", 'aria-label="Stored conversations"'],
    ["components/ai/conversation-view.tsx", 'aria-label="Conversation"'],
    ["components/ai/approval-panel.tsx", 'aria-label="Action awaiting approval"'],
  ];

  for (const [file, label] of labelled) {
    assert.ok(read(file).includes(label), `${file} is missing ${label}`);
  }
});

test("the sidebar sections are headed and associated with their headings", () => {
  const source = read("components/ai/ai-workspace.tsx");

  assert.ok(source.includes('aria-labelledby="console-agents"'));
  assert.ok(source.includes('aria-labelledby="console-conversations"'));
  assert.ok(source.includes('id="console-agents"'));
  assert.ok(source.includes('id="console-conversations"'));
});

test("the transcript announces new answers without re-reading itself", () => {
  const source = read("components/ai/conversation-view.tsx");

  assert.ok(source.includes('role="log"'));
  assert.ok(source.includes('aria-live="polite"'));
  assert.ok(source.includes("aria-busy"));
});

test("controls are real buttons with disabled states", () => {
  for (const file of [
    "components/console/agent-picker.tsx",
    "components/console/conversation-picker.tsx",
  ]) {
    const source = read(file);
    assert.ok(source.includes('type="button"'), `${file} needs explicit button types`);
    assert.ok(source.includes("disabled"), `${file} needs a disabled state`);
  }
});

test("an error code is reported as a status, not as decoration", () => {
  const source = read("components/console/execution-panel.tsx");

  assert.ok(source.includes('role="status"'));
});

// -- Responsive ---------------------------------------------------------------

test("the console is a stacking grid rather than a fixed-width layout", () => {
  const source = read("components/ai/ai-workspace.tsx");

  // One column by default, two from the large breakpoint up: the sidebar sits
  // above the transcript on a narrow viewport rather than beside it.
  assert.ok(source.includes("lg:grid-cols-["));
  assert.equal(source.includes("w-[1200px]"), false, "no fixed-width shell");
  assert.equal(/\bmin-w-\[\d{3,}px\]/.test(source), false, "nothing wider than a phone");
});

test("the execution figures reflow rather than overflowing", () => {
  const source = read("components/console/execution-panel.tsx");

  assert.ok(source.includes("grid-cols-2"), "two columns on a narrow viewport");
  assert.ok(source.includes("sm:grid-cols-4"), "four when there is room");
});

// -- Logging ------------------------------------------------------------------

test("the console logs nothing from the browser", () => {
  // A console.log of a run or a transcript puts a tenant's business data into
  // whatever is collecting browser logs, which is a different system with
  // different access rules.
  for (const file of CONSOLE_SOURCES) {
    const source = code(file);

    for (const forbidden of ["console.log", "console.debug", "console.info", "console.warn"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not call ${forbidden}`);
    }
  }
});

// -- The API the console speaks -----------------------------------------------

test("agents are listed from the server, scoped to the session", () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    void listAgents(caller);

    assert.equal(fetch.last.method, "GET");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/ai/agents");
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("an agent summary carries no instructions to leak", async () => {
  // The system prompt is the server's; publishing it would hand a caller the
  // text to work around. `AgentSummary` has no field for it.
  const fetch = stubFetch(() =>
    jsonResponse([{ id: "a", name: "Operations agent", description: "Answers questions." }]),
  );
  try {
    const [agent] = await listAgents(caller);

    assert.ok(agent);
    assert.deepEqual(Object.keys(agent).sort(), ["description", "id", "name"]);
    for (const forbidden of ["instructions", "system_prompt", "prompt", "model", "provider"]) {
      assert.equal(forbidden in agent, false, `an agent must not publish ${forbidden}`);
    }
  } finally {
    fetch.restore();
  }
});

test("conversations are listed from the server", () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    void listConversations(caller);

    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/ai/conversations");
  } finally {
    fetch.restore();
  }
});

test("a run response carries an execution summary and no payload", async () => {
  const run = agentRunResponse("Done.", ["get_shipment"]);

  // What the execution panel reads, and the complete set of it.
  assert.equal(typeof run.status, "string");
  assert.equal(typeof run.step_count, "number");
  assert.equal(typeof run.latency_ms, "number");
  assert.equal(typeof run.usage.total_tokens, "number");
  assert.deepEqual(
    run.tool_calls.map((call) => Object.keys(call).sort()),
    [["outcome", "tool_name"]],
  );

  for (const forbidden of ["messages", "arguments", "parameters", "input_data", "output_data"]) {
    assert.equal(forbidden in run, false, `a run must not carry ${forbidden}`);
  }
});

// -- Reconciling a durably cancelled run --------------------------------------
//
// The console can stop a run that is waiting on a person, and that is a *server*
// cancellation: the run ends and the approval is withdrawn. Two things have to
// be true for the screen to tell the truth afterwards, and the first one is
// easy to get wrong.

test("the reducer's cancelled action deliberately leaves a paused run paused", () => {
  // It exists for the client abandoning a generation, where un-pausing would be
  // a lie - a person is still being asked, whatever this page does. So the
  // console cannot use it to reflect a durable cancellation.
  const paused = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: awaitingApproval(),
  });

  const after = reduce(paused, { type: "cancelled" });

  assert.equal(after, paused, "a no-op, by design");
  assert.equal(isAwaitingApproval(after), true);
});

test("a run read back as cancelled clears the approval", () => {
  // Which is why the console rebuilds from the server instead: `loaded` derives
  // the paused state from the run, so an approval that no longer applies goes
  // with it. Without this, an approve button would stay on screen for a run
  // that no longer exists to approve.
  const paused = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: awaitingApproval(),
  });
  assert.equal(paused.approval !== null, true);

  const cancelled = reduce(paused, {
    type: "loaded",
    conversation: storedConversation(),
    run: { ...awaitingApproval(), status: "cancelled", approval: null },
  });

  assert.equal(cancelled.approval, null);
  assert.equal(isAwaitingApproval(cancelled), false);
});

test("stopping a run rebuilds from the server rather than nudging local state", () => {
  const source = code("components/ai/ai-workspace.tsx");
  const stop = source.slice(source.indexOf("const stop ="), source.indexOf("const abandon ="));

  assert.ok(stop.includes("cancelRun"));
  assert.ok(stop.includes("rebuildFrom"), "the outcome comes from the server");
  assert.equal(
    stop.includes('dispatch({ type: "cancelled" })'),
    false,
    "that action is a no-op on a paused run and would leave the approval on screen",
  );
});

test("refreshing and stopping share one way of rebuilding the view", () => {
  // Two paths that disagreed about what a moved-on run looks like is exactly
  // how one of them ends up showing a stale approval.
  const source = code("components/ai/ai-workspace.tsx");

  const between = (from: string, to: string): string =>
    source.slice(source.indexOf(from), source.indexOf(to));

  const reconcile = between("const reconcile =", "const stop =");
  const stop = between("const stop =", "const abandon =");

  assert.ok(reconcile.includes("rebuildFrom("), "refresh rebuilds from the server");
  assert.ok(stop.includes("rebuildFrom("), "stop rebuilds the same way");
  assert.ok(source.includes('dispatch({ type: "loaded"'), "the transcript is re-read, not patched");
});

test("a conversation created by the first run refreshes the sidebar once", () => {
  // Otherwise the thread an operator just started is missing from the list
  // beside it until something else reloads.
  const source = code("components/ai/ai-workspace.tsx");

  assert.ok(source.includes("knownConversation"));
  assert.ok(source.includes("onConversationsChanged()"));
});
