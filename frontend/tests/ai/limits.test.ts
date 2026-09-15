/**
 * The mirrored limits must match `app/schemas/ai.py` exactly.
 *
 * A drift guard, not a behaviour test. The frontend copy exists only to spare a
 * round trip, so it may never be *stricter* than the backend - that would make
 * the browser refuse input the server would have accepted - and never looser,
 * which would turn a helpful message into a 422.
 *
 * Verified against the backend schema on 2026-09-14:
 *
 *     MAX_MESSAGES            = 50
 *     MAX_MESSAGE_CHARACTERS  = 10_000
 *     MAX_TOTAL_CHARACTERS    = 50_000
 *     MAX_OUTPUT_TOKENS_LIMIT = 4_096   (not sent; server default applies)
 *     temperature             = 0.0 .. 2.0  (not sent; server default applies)
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_CONVERSATION,
  MAX_MESSAGES,
  MAX_MESSAGE_CHARACTERS,
  MAX_TOTAL_CHARACTERS,
  describeCapacity,
  toGenerateRequest,
} from "@/lib/ai/conversation";

test("the mirrored limits are the backend's", () => {
  assert.equal(MAX_MESSAGES, 50);
  assert.equal(MAX_MESSAGE_CHARACTERS, 10_000);
  assert.equal(MAX_TOTAL_CHARACTERS, 50_000);
});

test("the client never weakens the backend's validation", () => {
  // One under each limit is accepted here and would be accepted there too.
  const justUnder = "x".repeat(MAX_MESSAGE_CHARACTERS);
  assert.equal(describeCapacity(EMPTY_CONVERSATION, justUnder).canSend, true);

  // One over is refused here, which is the whole point of mirroring.
  const justOver = "x".repeat(MAX_MESSAGE_CHARACTERS + 1);
  assert.equal(describeCapacity(EMPTY_CONVERSATION, justOver).canSend, false);
});

test("a full conversation offers starting over rather than dropping messages", () => {
  const turns = Array.from({ length: MAX_MESSAGES }, (_, index) => ({
    id: `t-${index}`,
    role: "user" as const,
    content: "x",
  }));
  const capacity = describeCapacity({ ...EMPTY_CONVERSATION, turns }, "more");

  assert.equal(capacity.full, true);
  assert.match(capacity.reason ?? "", /Start a new one/);
  // Nothing is trimmed or discarded on the client's initiative.
  assert.equal(toGenerateRequest(turns).messages.length, MAX_MESSAGES);
});

test("no output limit or sampling value is ever sent", () => {
  // Both are server policy; a client that could set them could widen them.
  const body = toGenerateRequest([{ id: "t1", role: "user", content: "Hello" }]);
  assert.deepEqual(Object.keys(body), ["messages"]);
});
