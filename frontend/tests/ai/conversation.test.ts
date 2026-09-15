/**
 * The conversation: what it accepts, what it refuses, and what a failure
 * leaves behind.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  agentRunResponse,
  awaitingApproval,
  CONVERSATION_ID,
  storedConversation,
} from "../support/fixtures";
import {
  EMPTY_CONVERSATION,
  MAX_MESSAGES,
  MAX_MESSAGE_CHARACTERS,
  MAX_TOTAL_CHARACTERS,
  canRetry,
  conversationReducer as reduce,
  describeCapacity,
  isAwaitingApproval,
  newIdempotencyKey,
  pendingIdempotencyKey,
  toAgentRunRequest,
  type ConversationState,
  type Turn,
} from "@/lib/ai/conversation";

/** Type a message, the way the composer does. */
function send(state: ConversationState, content: string, key = "key-1"): ConversationState {
  return reduce(state, {
    type: "send",
    id: `u-${state.turns.length}`,
    content,
    idempotencyKey: key,
  });
}

/** Send a message and answer it, the way the workspace does. */
function exchange(state: ConversationState, ask: string, answer: string): ConversationState {
  const sent = send(state, ask);
  return reduce(sent, {
    type: "received",
    id: `a-${sent.turns.length}`,
    response: agentRunResponse(answer),
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
  const state = send(EMPTY_CONVERSATION, "   ");
  assert.equal(state, EMPTY_CONVERSATION, "the state is not even touched");
});

// -- Sending ------------------------------------------------------------------

test("sending shows the user's message immediately and starts generating", () => {
  const state = send(EMPTY_CONVERSATION, "Hello");

  assert.equal(state.turns.length, 1);
  assert.deepEqual(state.turns[0], {
    id: "u-0",
    role: "user",
    content: "Hello",
    // Carried on the turn, so a retry of *this* message sends the same key.
    idempotencyKey: "key-1",
  });
  assert.equal(state.status, "generating");
});

test("the message is trimmed before it is recorded or sent", () => {
  const state = send(EMPTY_CONVERSATION, "  Hello  ");
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

test("the answer carries what the run reported about it", () => {
  const state = exchange(EMPTY_CONVERSATION, "Hello", "Hi.");
  const answer = state.turns[1]!;

  assert.equal(answer.role === "assistant" && answer.usage?.total_tokens, 30);
  assert.equal(answer.role === "assistant" && answer.latencyMs, 412.5);
});

test("the answer records which tools the agent used", () => {
  const sent = send(EMPTY_CONVERSATION, "Where is ABC123?");
  const state = reduce(sent, {
    type: "received",
    id: "a1",
    response: agentRunResponse("In transit.", ["get_shipment", "get_shipment_charges"]),
  });

  const answer = state.turns[1]!;
  assert.deepEqual(
    answer.role === "assistant" ? answer.tools : [],
    ["get_shipment", "get_shipment_charges"],
  );
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
  const generating = send(EMPTY_CONVERSATION, "First");
  const again = send(generating, "Second");

  assert.equal(again, generating, "the second submission changes nothing");
  assert.equal(again.turns.length, 1);
});

test("the composer reports that it is busy rather than refusing with a reason", () => {
  const generating = send(EMPTY_CONVERSATION, "First");
  const capacity = describeCapacity(generating, "Second");

  assert.equal(capacity.canSend, false);
  assert.equal(capacity.reason, null);
});

test("an answer arriving when nothing was asked is ignored", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "received",
    id: "a1",
    response: agentRunResponse("Unasked for."),
  });
  assert.equal(state, EMPTY_CONVERSATION);
});

// -- Failure ------------------------------------------------------------------

test("a failure keeps every earlier message and the one just typed", () => {
  const boom = new Error("network");
  let state = exchange(EMPTY_CONVERSATION, "First", "One.");
  state = send(state, "Second");
  state = reduce(state, { type: "failed", error: boom });

  assert.equal(state.turns.length, 3, "two from the first exchange, plus the unanswered one");
  assert.equal(state.turns[2]!.content, "Second");
  assert.equal(state.status, "idle");
  assert.equal(state.error, boom);
});

test("a failed conversation can be retried", () => {
  let state = send(EMPTY_CONVERSATION, "Hello");
  state = reduce(state, { type: "failed", error: new Error("boom") });

  assert.equal(canRetry(state), true);
});

test("a retry does not add the user's message a second time", () => {
  let state = send(EMPTY_CONVERSATION, "Hello");
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "retry" });

  assert.equal(state.turns.length, 1);
  assert.equal(state.status, "generating");
  assert.equal(state.error, null, "the previous failure is cleared while trying again");
});

test("a retry that succeeds answers the original message", () => {
  let state = send(EMPTY_CONVERSATION, "Hello");
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "retry" });
  state = reduce(state, { type: "received", id: "a1", response: agentRunResponse("Hi.") });

  assert.equal(state.turns.length, 2);
  assert.equal(state.error, null);
});

test("there is nothing to retry once an answer has arrived", () => {
  assert.equal(canRetry(exchange(EMPTY_CONVERSATION, "Hello", "Hi.")), false);
  assert.equal(canRetry(EMPTY_CONVERSATION), false);
});

test("the next send clears the previous error", () => {
  let state = send(EMPTY_CONVERSATION, "Hello");
  state = reduce(state, { type: "failed", error: new Error("boom") });
  state = reduce(state, { type: "received", id: "a1", response: agentRunResponse("Hi.") });
  assert.equal(state.status, "idle", "an answer for an abandoned attempt is ignored");

  state = reduce(state, { type: "retry" });
  assert.equal(state.error, null);
});

// -- Cancelling ---------------------------------------------------------------

test("cancelling stops the loading state without an error", () => {
  let state = send(EMPTY_CONVERSATION, "Hello");
  state = reduce(state, { type: "cancelled" });

  assert.equal(state.status, "idle");
  assert.equal(state.error, null, "stopping on purpose is not a failure");
});

test("cancelling invents no assistant message and keeps the conversation", () => {
  let state = exchange(EMPTY_CONVERSATION, "First", "One.");
  state = send(state, "Second");
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

test("the request carries one new message, not the history", () => {
  // The conversation is the server's. Re-sending it would be a client asserting
  // what was already said, and the backend refuses anything but a single new
  // user turn on a conversation that already exists.
  const state = send(exchange(EMPTY_CONVERSATION, "First", "One."), "Second");

  assert.deepEqual(toAgentRunRequest(state)?.messages, [{ role: "user", content: "Second" }]);
});

test("a continued conversation names which one", () => {
  const first = exchange(EMPTY_CONVERSATION, "First", "One.");
  const state = send(first, "Second");

  assert.equal(state.conversationId, CONVERSATION_ID);
  assert.equal(toAgentRunRequest(state)?.conversation_id, CONVERSATION_ID);
});

test("the first message names no conversation, because there is none yet", () => {
  const body = toAgentRunRequest(send(EMPTY_CONVERSATION, "Hello"));

  assert.deepEqual(Object.keys(body ?? {}), ["messages"]);
});

test("there is nothing to send when nobody has asked anything", () => {
  assert.equal(toAgentRunRequest(EMPTY_CONVERSATION), null);
  assert.equal(toAgentRunRequest(exchange(EMPTY_CONVERSATION, "Hi", "Hello.")), null);
});

test("the request names no model, so the server's configured one is used", () => {
  // Nothing exposes the allow-list, so the browser has no legitimate way to
  // choose - and cannot route around LLM_ALLOWED_MODELS by trying.
  const body = toAgentRunRequest(send(EMPTY_CONVERSATION, "Hello"));
  assert.equal("model" in (body ?? {}), false);
});

test("the request can carry no system message", () => {
  // Structural: the role type has no such member, so an agent's instructions
  // cannot be overridden from here even by mistake.
  const body = toAgentRunRequest(send(EMPTY_CONVERSATION, "Hello"));
  const roles = new Set((body?.messages ?? []).map((message) => message.role));

  assert.deepEqual([...roles], ["user"]);
});

test("the request sets no sampling or output limits", () => {
  // Those are server policy. Sending them would only let a client widen them.
  const body = toAgentRunRequest(send(EMPTY_CONVERSATION, "Hello")) ?? {};
  assert.equal("temperature" in body, false);
  assert.equal("max_output_tokens" in body, false);
});

test("the request carries no organization or tenant field", () => {
  // The organization comes from the verified membership on the server side.
  const body = toAgentRunRequest(send(EMPTY_CONVERSATION, "Hello")) ?? {};
  assert.equal("organization_id" in body, false);
  assert.equal("tenant_id" in body, false);
});

// -- Idempotency --------------------------------------------------------------

test("a message carries a key, and a retry sends the same one", () => {
  // Which is what makes retrying safe: the backend hands back the run it
  // already made rather than starting a second one.
  const sent = send(EMPTY_CONVERSATION, "Cancel ABC123", "key-abc");
  const retried = reduce(reduce(sent, { type: "failed", error: new Error("no") }), {
    type: "retry",
  });

  assert.equal(pendingIdempotencyKey(sent), "key-abc");
  assert.equal(pendingIdempotencyKey(retried), "key-abc", "the same turn, the same key");
});

test("two different messages get different keys", () => {
  assert.notEqual(newIdempotencyKey(), newIdempotencyKey());
});

test("an answered conversation has no key pending", () => {
  assert.equal(pendingIdempotencyKey(exchange(EMPTY_CONVERSATION, "Hi", "Hello.")), undefined);
});

// -- Waiting on a person ------------------------------------------------------

test("a run that needs approval records no answer", () => {
  const sent = send(EMPTY_CONVERSATION, "Cancel ABC123");

  const paused = reduce(sent, { type: "received", id: "a1", response: awaitingApproval() });

  assert.equal(paused.status, "awaiting_approval");
  assert.equal(paused.turns.length, 1, "nothing has been answered");
  assert.equal(paused.approval?.tool_name, "cancel_shipment");
  assert.equal(paused.error, null, "being asked is not a failure");
});

test("nothing can be sent while somebody is deciding", () => {
  const paused = reduce(send(EMPTY_CONVERSATION, "Cancel ABC123"), {
    type: "received",
    id: "a1",
    response: awaitingApproval(),
  });

  const capacity = describeCapacity(paused, "Actually, never mind");

  assert.equal(capacity.canSend, false);
  assert.match(capacity.reason ?? "", /waiting for somebody to approve/);
  assert.equal(reduce(paused, { type: "send", id: "u2", content: "x", idempotencyKey: "k" }), paused);
});

test("approving puts the conversation back to work", () => {
  const paused = reduce(send(EMPTY_CONVERSATION, "Cancel ABC123"), {
    type: "received",
    id: "a1",
    response: awaitingApproval(),
  });

  const deciding = reduce(paused, { type: "deciding" });

  assert.equal(deciding.status, "generating");
});

test("the resumed run's answer lands in the same conversation", () => {
  const paused = reduce(send(EMPTY_CONVERSATION, "Cancel ABC123"), {
    type: "received",
    id: "a1",
    response: awaitingApproval(),
  });
  const deciding = reduce(paused, { type: "deciding" });

  const done = reduce(deciding, {
    type: "received",
    id: "a2",
    response: agentRunResponse("A person declined, so nothing was cancelled.", [
      "cancel_shipment",
    ]),
  });

  assert.equal(done.status, "idle");
  assert.equal(done.approval, null);
  assert.equal(done.conversationId, paused.conversationId, "the same conversation");
  assert.equal(done.turns.at(-1)?.content, "A person declined, so nothing was cancelled.");
});

test("stopping the page does not un-pause a run somebody is deciding", () => {
  // The person is still being asked, whatever this browser does next.
  const paused = reduce(send(EMPTY_CONVERSATION, "Cancel ABC123"), {
    type: "received",
    id: "a1",
    response: awaitingApproval(),
  });

  assert.equal(reduce(paused, { type: "cancelled" }), paused);
});

// -- A run that ended without an answer --------------------------------------

test("an abandoned run is reported in words the user can act on", () => {
  const sent = send(EMPTY_CONVERSATION, "Hello");

  const state = reduce(sent, {
    type: "received",
    id: "a1",
    response: {
      ...agentRunResponse(""),
      status: "failed",
      final_response: null,
      error_code: "agent_run_abandoned",
    },
  });

  assert.equal(state.status, "idle");
  assert.equal(state.turns.length, 1, "no answer was invented");
  assert.match(String(state.error), /stopped before it finished/);
});

// -- Reading a stored conversation back ---------------------------------------

test("a stored conversation comes back as the turns it was", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: null,
  });

  assert.equal(state.conversationId, CONVERSATION_ID);
  assert.equal(state.status, "idle");
  assert.deepEqual(
    state.turns.map((turn) => turn.role),
    ["user", "assistant"],
  );
  assert.equal(state.turns[0]?.content, "Where is ABC123?");
});

test("stored tool turns become the tool names on the answer that followed", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: null,
  });

  const answer = state.turns[1];
  assert.equal(answer?.role, "assistant");
  assert.deepEqual(answer?.role === "assistant" ? answer.tools : [], ["get_shipment"]);
});

test("a reloaded page finds a run that is still waiting on a person", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: awaitingApproval(),
  });

  assert.equal(state.status, "awaiting_approval");
  assert.equal(state.approval?.tool_name, "cancel_shipment");
  assert.equal(isAwaitingApproval(state), true);
});

test("a stored turn carries no usage to invent a cost from", () => {
  const state = reduce(EMPTY_CONVERSATION, {
    type: "loaded",
    conversation: storedConversation(),
    run: null,
  });

  const answer = state.turns[1];
  assert.equal(answer?.role === "assistant" ? answer.usage : "missing", undefined);
});
