/**
 * How the console describes an execution state.
 *
 * The point of this module is that the vocabulary is the *backend's*. So the
 * tests that matter are the ones asserting the maps are total over the union -
 * a status added to `AgentRunStatus` must be a compile error and a test failure
 * here, not a blank badge in production - and that nothing is described by
 * colour alone.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  describeDuration,
  describeErrorCode,
  describeTool,
  isCancellable,
  isInFlight,
  isTerminal,
  RUN_LABELS,
  RUN_TONES,
  TOOL_LABELS,
  TOOL_MARKS,
  TOOL_TONES,
} from "@/lib/ai/run-status";
import type { AgentRunStatus, ToolOutcome } from "@/types/ai";

// Written out rather than derived, so that adding a backend status and
// forgetting the console is a failure here rather than a silent gap.
const RUN_STATUSES: AgentRunStatus[] = [
  "pending",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
];

const TOOL_OUTCOMES: ToolOutcome[] = [
  "succeeded",
  "failed",
  "timed_out",
  "cancelled",
  "approval_required",
  "rejected",
];

// -- The vocabulary is complete -----------------------------------------------

test("every run status has a label and a tone", () => {
  assert.deepEqual(Object.keys(RUN_LABELS).sort(), [...RUN_STATUSES].sort());
  assert.deepEqual(Object.keys(RUN_TONES).sort(), [...RUN_STATUSES].sort());
});

test("every tool outcome has a label, a tone and a mark", () => {
  assert.deepEqual(Object.keys(TOOL_LABELS).sort(), [...TOOL_OUTCOMES].sort());
  assert.deepEqual(Object.keys(TOOL_TONES).sort(), [...TOOL_OUTCOMES].sort());
  assert.deepEqual(Object.keys(TOOL_MARKS).sort(), [...TOOL_OUTCOMES].sort());
});

test("no state is described by colour alone", () => {
  // A tone is a hint. The word is the answer, and it is what a screen reader
  // and a monochrome display get.
  for (const status of RUN_STATUSES) {
    assert.ok(RUN_LABELS[status].length > 0, status);
  }
  for (const outcome of TOOL_OUTCOMES) {
    assert.ok(TOOL_LABELS[outcome].length > 0, outcome);
  }
});

test("the labels are words an operator would use, not enum spellings", () => {
  assert.equal(RUN_LABELS.awaiting_approval, "Waiting for approval");
  assert.equal(TOOL_LABELS.timed_out, "timed out");
  assert.equal(TOOL_LABELS.approval_required, "waiting for approval");
});

// -- What each state means ----------------------------------------------------

test("only pending and running are still moving on their own", () => {
  assert.equal(isInFlight("pending"), true);
  assert.equal(isInFlight("running"), true);

  // The important one: a paused run changes when a person decides, which is an
  // action rather than the passage of time. Treating it as in-flight is what
  // would justify polling it.
  assert.equal(isInFlight("awaiting_approval"), false);
  assert.equal(isInFlight("completed"), false);
});

test("terminal states are the three a run never leaves", () => {
  assert.deepEqual(
    RUN_STATUSES.filter(isTerminal),
    ["completed", "failed", "cancelled"],
  );
});

test("awaiting_approval is not terminal", () => {
  // It is a pause, not an ending: the same run continues once somebody answers.
  assert.equal(isTerminal("awaiting_approval"), false);
});

test("only a paused run may be offered a stop control", () => {
  // Mirrors the backend rule exactly. A run that is running is being advanced
  // by another request; a finished one has nothing to stop. Both answer 409,
  // so offering a button for them would promise something already refused.
  assert.deepEqual(RUN_STATUSES.filter(isCancellable), ["awaiting_approval"]);
});

// -- Tool presentation --------------------------------------------------------

test("a finished tool call is described by its outcome", () => {
  const shown = describeTool({ tool_name: "get_shipment", outcome: "succeeded" });

  assert.equal(shown.label, "succeeded");
  assert.equal(shown.tone, "ok");
  assert.equal(shown.mark, "✓");
});

test("a refused tool call is not described as a failure", () => {
  // A person declining an action is not an infrastructure fault, and the
  // console must not present it as one.
  const shown = describeTool({ tool_name: "cancel_shipment", outcome: "rejected" });

  assert.equal(shown.label, "declined");
  assert.notEqual(shown.tone, "danger");
});

test("a tool call with no recorded outcome reads as running", () => {
  const shown = describeTool({ tool_name: "get_shipment", outcome: null });

  assert.equal(shown.label, "running");
  assert.equal(shown.tone, "accent");
});

test("every outcome produces a presentation", () => {
  for (const outcome of TOOL_OUTCOMES) {
    const shown = describeTool({ tool_name: "t", outcome });
    assert.ok(shown.label && shown.mark && shown.tone, outcome);
  }
});

// -- Numbers ------------------------------------------------------------------

test("a duration is given in the unit somebody would read it in", () => {
  assert.equal(describeDuration(0), "0 ms");
  assert.equal(describeDuration(12), "12 ms");
  assert.equal(describeDuration(999), "999 ms");
  assert.equal(describeDuration(1_500), "1.5 s");
  assert.equal(describeDuration(59_000), "59.0 s");
  assert.equal(describeDuration(90_000), "1m 30s");
});

test("an impossible duration is a dash rather than a wrong number", () => {
  assert.equal(describeDuration(-1), "–");
  assert.equal(describeDuration(Number.NaN), "–");
});

test("an error code is read back as a sentence", () => {
  assert.equal(describeErrorCode("agent_run_abandoned"), "Agent run abandoned");
  assert.equal(describeErrorCode("approval_expired"), "Approval expired");
});

test("an error code is shown as itself, never as a stack trace", () => {
  // The backend publishes a stable code and a client-safe sentence. Neither is
  // a traceback, and this module has no branch that could produce one.
  const shown = describeErrorCode("llm_provider_error");

  assert.equal(shown, "Llm provider error");
  assert.ok(!shown.includes("Traceback"));
  assert.ok(!shown.includes("File \""));
});
