/**
 * The conversation: what it accepts, what it refuses, and what a failure
 * leaves behind.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { generateResponse } from "../support/fixtures";
import {
  EMPTY_CONVERSATION,
  MAX_MESSAGES,
  MAX_MESSAGE_CHARACTERS,
  MAX_TOTAL_CHARACTERS,
  canRetry,
  conversationReducer as reduce,
  describeCapacity,
  toGenerateRequest,
  type ConversationState,
  type Turn,
} from "@/lib/ai/conversation";

/** Send a message and answer it, the way the workspace does. */
function exchange(state: ConversationState, ask: string, answer: string): ConversationState {
  const sent = reduce(state, { type: "send", id: `u-${state.turns.length}`, content: ask });
  return reduce(sent, {
    type: "received",
    id: `a-${sent.turns.length}`,
    response: generateResponse(answer),
  });
}

function userTurns(count: number, content = "x"): Turn[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `t-${index}`,
    role: "user" as const,
    content,
  }));
}

// -- The empty workspace ------------------------------------------------------

test("a new conversation is empty and idle", () => {
  assert.deepEqual(EMPTY_CONVERSATION.turns, []);
  assert.equal(EMPTY_CONVERSATION.status, "idle");
  assert.equal(EMPTY_CONVERSATION.error, null);
});

test("nothing can be sent from an empty composer", () => {
  const capacity = describeCapacity(EMPTY_CONVERSATION, "");
  assert.equal(capacity.canSend, false);
  // Not an error: an empty box is the ordinary state, not a complaint.
  assert.equal(capacity.reason, null);
});

test("whitespace alone is not a message", () => {
  assert.equal(describeCapacity(EMPTY_CONVERSATION, "   \n\t ").canSend, false);
  const state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "   " });
  assert.equal(state, EMPTY_CONVERSATION, "the state is not even touched");
});

// -- Sending ------------------------------------------------------------------

test("sending shows the user's message immediately and starts generating", () => {
  const state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });

  assert.equal(state.turns.length, 1);
  assert.deepEqual(state.turns[0], { id: "u1", role: "user", content: "Hello" });
  assert.equal(state.status, "generating");
});

test("the message is trimmed before it is recorded or sent", () => {
  const state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "  Hello  " });
  assert.equal(state.turns[0]!.content, "Hello");
});

test("the answer is appended and the composer is released", () => {
  const state = exchange(EMPTY_CONVERSATION, "Hello", "Hi there.");

  assert.equal(state.turns.length, 2);
  const answer = state.turns[1]!;
  assert.equal(answer.role, "assistant");
  assert.equal(answer.content, "Hi there.");
  assert.equal(state.status, "idle");
});

test("the answer carries what the endpoint reported about it", () => {
  const state = exchange(EMPTY_CONVERSATION, "Hello", "Hi.");
  const answer = state.turns[1]!;

  assert.equal(answer.role === "assistant" && answer.model, "claude-opus-5");
  assert.equal(answer.role === "assistant" && answer.usage.total_tokens, 30);
  assert.equal(answer.role === "assistant" && answer.latencyMs, 412.5);
});

test("a conversation accumulates turns in order", () => {
  let state = exchange(EMPTY_CONVERSATION, "First", "One.");
  state = exchange(state, "Second", "Two.");
  state = exchange(state, "Third", "Three.");

  assert.deepEqual(
    state.turns.map((turn) => `${turn.role}:${turn.content}`),
    ["user:First", "assistant:One.", "user:Second", "assistant:Two.", "user:Third", "assistant:Three."],
  );
});

// -- Double submission --------------------------------------------------------

test("a second message cannot be sent while one is generating", () => {
  const generating = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "First" });
  const again = reduce(generating, { type: "send", id: "u2", content: "Second" });

  assert.equal(again, generating, "the second submission changes nothing");
  assert.equal(again.turns.length, 1);
});

test("the composer reports that it is busy rather than refusing with a reason", () => {
  const generating = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "First" });
  const capacity = describeCapacity(generating, "Second");

  assert.equal(capacity.canSend, false);
  assert.equal(capacity.reason, null);
});

test("an answer arriving when nothing was asked is ignored", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "received",
    id: "a1",
    response: generateResponse("Unasked for."),
  });
  assert.equal(state, EMPTY_CONVERSATION);
});

// -- Failure ------------------------------------------------------------------

test("a failure keeps every earlier message and the one just typed", () => {
  const boom = new Error("network");
  let state = exchange(EMPTY_CONVERSATION, "First", "One.");
  state = reduce(state, { type: "send", id: "u2", content: "Second" });
  state = reduce(state, { type: "failed", error: boom });

  assert.equal(state.turns.length, 3, "two from the first exchange, plus the unanswered one");
  assert.equal(state.turns[2]!.content, "Second");
  assert.equal(state.status, "idle");
  assert.equal(state.error, boom);
});

test("a failed conversation can be retried", () => {
  let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });
  state = reduce(state, { type: "failed", error: new Error("boom") });

  assert.equal(canRetry(state), true);
});

test("a retry does not add the user's message a second time", () => {
  let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "retry" });

  assert.equal(state.turns.length, 1);
  assert.equal(state.status, "generating");
  assert.equal(state.error, null, "the previous failure is cleared while trying again");
});

test("a retry that succeeds answers the original message", () => {
  let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "retry" });
  state = reduce(state, { type: "received", id: "a1", response: generateResponse("Hi.") });

  assert.equal(state.turns.length, 2);
  assert.equal(state.error, null);
});

test("there is nothing to retry once an answer has arrived", () => {
  assert.equal(canRetry(exchange(EMPTY_CONVERSATION, "Hello", "Hi.")), false);
  assert.equal(canRetry(EMPTY_CONVERSATION), false);
});

test("the next send clears the previous error", () => {
  let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "received", id: "a1", response: generateResponse("Hi.") });
  assert.equal(state.status, "idle", "an answer for an abandoned attempt is ignored");

  state = reduce(state, { type: "retry" });
  assert.equal(state.error, null);
});

// -- Cancelling ---------------------------------------------------------------

test("cancelling stops the loading state without an error", () => {
  let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });
  state = reduce(state, { type: "cancelled" });

  assert.equal(state.status, "idle");
  assert.equal(state.error, null, "stopping on purpose is not a failure");
});

test("cancelling invents no assistant message and keeps the conversation", () => {
  let state = exchange(EMPTY_CONVERSATION, "First", "One.");
  state = reduce(state, { type: "send", id: "u2", content: "Second" });
  state = reduce(state, { type: "cancelled" });

  assert.equal(state.turns.length, 3);
  assert.equal(state.turns.at(-1)!.role, "user");
  assert.equal(canRetry(state), true, "and the cancelled message can be sent again");
});

test("starting over empties the conversation", () => {
  const state = reduce(exchange(EMPTY_CONVERSATION, "Hello", "Hi."), { type: "reset" });
  assert.deepEqual(state, EMPTY_CONVERSATION);
});

// -- The backend's limits -----------------------------------------------------

test("a message longer than the backend allows is refused with a reason", () => {
  const capacity = describeCapacity(EMPTY_CONVERSATION, "x".repeat(MAX_MESSAGE_CHARACTERS + 1));

  assert.equal(capacity.canSend, false);
  assert.match(capacity.reason ?? "", /at most 10,000 characters/);
  assert.equal(capacity.full, false, "a shorter message would still work");
});

test("a message at exactly the limit is allowed", () => {
  assert.equal(
    describeCapacity(EMPTY_CONVERSATION, "x".repeat(MAX_MESSAGE_CHARACTERS)).canSend,
    true,
  );
});

test("a conversation at the message limit says to start a new one", () => {
  const state = { ...EMPTY_CONVERSATION, turns: userTurns(MAX_MESSAGES) };
  const capacity = describeCapacity(state, "One more");

  assert.equal(capacity.canSend, false);
  assert.equal(capacity.full, true);
  assert.match(capacity.reason ?? "", /Start a new one/);
});

test("a conversation at the size limit says to start a new one", () => {
  const state = { ...EMPTY_CONVERSATION, turns: userTurns(5, "x".repeat(MAX_TOTAL_CHARACTERS / 5)) };
  const capacity = describeCapacity(state, "One more");

  assert.equal(capacity.canSend, false);
  assert.equal(capacity.full, true);
});

test("a message that would push the conversation over the size limit is refused", () => {
  const state = { ...EMPTY_CONVERSATION, turns: userTurns(1, "x".repeat(MAX_TOTAL_CHARACTERS - 10)) };
  const capacity = describeCapacity(state, "x".repeat(20));

  assert.equal(capacity.canSend, false);
  assert.equal(capacity.full, false, "this one is about the draft, not the conversation");
  assert.match(capacity.reason ?? "", /Shorten it/);
});

// -- The request body ---------------------------------------------------------

test("the request is the conversation, oldest first", () => {
  const state = exchange(exchange(EMPTY_CONVERSATION, "First", "One."), "Second", "Two.");

  assert.deepEqual(toGenerateRequest(state.turns), {
    messages: [
      { role: "user", content: "First" },
      { role: "assistant", content: "One." },
      { role: "user", content: "Second" },
      { role: "assistant", content: "Two." },
    ],
  });
});

test("the request names no model, so the server's configured one is used", () => {
  // Nothing exposes the allow-list, so the browser has no legitimate way to
  // choose - and cannot route around LLM_ALLOWED_MODELS by trying.
  const body = toGenerateRequest(exchange(EMPTY_CONVERSATION, "Hello", "Hi.").turns);
  assert.equal("model" in body, false);
});

test("the request carries no system message", () => {
  const body = toGenerateRequest(exchange(EMPTY_CONVERSATION, "Hello", "Hi.").turns);
  assert.equal(
    body.messages.some((message) => message.role === "system"),
    false,
  );
});

test("the request sets no sampling or output limits", () => {
  // Those are server policy. Sending them would only let a client widen them.
  const body = toGenerateRequest(exchange(EMPTY_CONVERSATION, "Hello", "Hi.").turns);
  assert.equal("temperature" in body, false);
  assert.equal("max_output_tokens" in body, false);
});

test("the request carries no organization or tenant field", () => {
  // The organization comes from the verified membership on the server side.
  const body = toGenerateRequest(exchange(EMPTY_CONVERSATION, "Hello", "Hi.").turns);
  assert.deepEqual(Object.keys(body), ["messages"]);
});
