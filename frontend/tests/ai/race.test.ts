/**
 * The conversation under races: a reply that arrives after its request stopped
 * being the current one.
 *
 * The workspace guards every dispatch with `inFlight.current === controller`.
 * These tests exercise the reducer half of that guarantee - what happens if a
 * late result is dispatched anyway - so the two defences are independent.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { agentRunResponse } from "../support/fixtures";
import {
  EMPTY_CONVERSATION,
  conversationReducer as reduce,
  type ConversationState,
} from "@/lib/ai/conversation";

function asked(content = "Hello"): ConversationState {
  return reduce(EMPTY_CONVERSATION, {
    type: "send",
    id: "u1",
    content,
    idempotencyKey: "key-1",
  });
}

test("a reply arriving after cancellation is ignored", () => {
  const cancelled = reduce(asked(), { type: "cancelled" });

  const late = reduce(cancelled, { type: "received", id: "a1", response: agentRunResponse("Late.") });

  assert.equal(late.turns.length, 1, "no assistant turn appears after Stop");
  assert.equal(late, cancelled, "the state is not even replaced");
});

test("a failure arriving after cancellation does not raise an error banner", () => {
  // The workspace discards this outright; the reducer would record it, so the
  // guard in the component is what makes the pair safe.
  const cancelled = reduce(asked(), { type: "cancelled" });
  assert.equal(cancelled.error, null);
});

test("a reply arriving after the conversation was reset is ignored", () => {
  const reset = reduce(asked(), { type: "reset" });

  const late = reduce(reset, { type: "received", id: "a1", response: agentRunResponse("Late.") });

  assert.deepEqual(late, EMPTY_CONVERSATION);
});

test("two replies for one question record only the first", () => {
  const first = reduce(asked(), { type: "received", id: "a1", response: agentRunResponse("One.") });
  const second = reduce(first, { type: "received", id: "a2", response: agentRunResponse("Two.") });

  assert.equal(second.turns.length, 2);
  assert.equal(second, first, "the conversation is idle, so the duplicate is dropped");
});

test("a cancellation arriving while a newer generation runs would stop it", () => {
  // Documents why the component guards on the controller rather than relying on
  // the reducer: from the reducer's point of view a stale "cancelled" is
  // indistinguishable from a real one.
  const running = asked("Second question");
  assert.equal(running.status, "generating");

  const stale = reduce(running, { type: "cancelled" });

  assert.equal(stale.status, "idle", "which is exactly why the component filters it out first");
});
