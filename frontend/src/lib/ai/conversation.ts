/**
 * The AI workspace's conversation, as plain data and pure functions.
 *
 * No React and no network, for the same reason the session is: it can be
 * tested without a DOM, and the rules about what may be sent live in one
 * readable place rather than inside a component.
 *
 * The conversation is **browser memory only**. There is no conversation
 * endpoint, no persistence, and no server-side conversation id - the backend's
 * generate endpoint is stateless and takes the whole message list every time.
 * Reloading the page therefore starts a new conversation, consistent with the
 * in-memory session.
 */

import type { AIMessage, AIUsage, GenerateRequest, GenerateResponse } from "@/types/ai";

/**
 * Limits mirrored from `app/schemas/ai.py`.
 *
 * Here so the interface can say "this conversation is full" before spending a
 * round trip on a 422. They may not be stricter than the backend's, and the
 * backend re-checks all of them regardless.
 */
export const MAX_MESSAGES = 50;
export const MAX_MESSAGE_CHARACTERS = 10_000;
export const MAX_TOTAL_CHARACTERS = 50_000;

/**
 * A unique id for a turn.
 *
 * A counter rather than a UUID: these are React keys within one conversation,
 * they never leave the browser, and a counter is deterministic in tests.
 */
let turnCounter = 0;

export function nextTurnId(): string {
  turnCounter += 1;
  return `turn-${turnCounter}`;
}

export interface UserTurn {
  id: string;
  role: "user";
  content: string;
}

export interface AssistantTurn {
  id: string;
  role: "assistant";
  content: string;
  /** Which model answered. Reported by the backend; the provider is not. */
  model: string;
  usage: AIUsage;
  latencyMs: number;
}

export type Turn = UserTurn | AssistantTurn;

export interface ConversationState {
  turns: Turn[];
  /** "generating" blocks a second submission until the first settles. */
  status: "idle" | "generating";
  /** The last failure, for the view to present. Cleared on the next attempt. */
  error: unknown;
}

export const EMPTY_CONVERSATION: ConversationState = {
  turns: [],
  status: "idle",
  error: null,
};

export type ConversationAction =
  | { type: "send"; id: string; content: string }
  | { type: "retry" }
  | { type: "received"; id: string; response: GenerateResponse }
  | { type: "failed"; error: unknown }
  | { type: "cancelled" }
  | { type: "reset" };

/**
 * The conversation's only state transitions.
 *
 * Three rules are enforced here rather than in the view, because the view can
 * be raced by a fast second click and this cannot:
 *
 * * nothing starts while a generation is in flight;
 * * a failure keeps every turn already in the conversation;
 * * a retry re-sends what is already there instead of adding the user's
 *   message a second time.
 */
export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case "send": {
      const content = action.content.trim();
      // Both guards matter: an empty message is rejected by the backend, and a
      // second submission mid-flight would interleave two answers.
      if (!content || state.status === "generating") return state;

      return {
        turns: [...state.turns, { id: action.id, role: "user", content }],
        status: "generating",
        error: null,
      };
    }

    case "retry": {
      // Only meaningful when the conversation is waiting on an answer that
      // never came, which is exactly when the last turn is the user's.
      if (state.status === "generating" || !awaitingAnswer(state)) return state;
      return { ...state, status: "generating", error: null };
    }

    case "received": {
      if (state.status !== "generating") return state;

      const { content, model, usage, latency_ms } = action.response;
      return {
        turns: [
          ...state.turns,
          { id: action.id, role: "assistant", content, model, usage, latencyMs: latency_ms },
        ],
        status: "idle",
        error: null,
      };
    }

    case "failed":
      // The turns are untouched: a failed request must not cost the user the
      // conversation so far, nor the message they just typed.
      return { ...state, status: "idle", error: action.error };

    case "cancelled":
      // Stopping on purpose is not a failure, so no error is recorded and no
      // assistant turn is invented.
      return { ...state, status: "idle", error: null };

    case "reset":
      return EMPTY_CONVERSATION;

    default:
      return state;
  }
}

/** Whether the last thing said was the user's, with no answer yet. */
export function awaitingAnswer(state: ConversationState): boolean {
  return state.turns.at(-1)?.role === "user";
}

/** Whether a failed or cancelled attempt can be tried again. */
export function canRetry(state: ConversationState): boolean {
  return state.status === "idle" && awaitingAnswer(state);
}

// -- What may be sent ---------------------------------------------------------

export interface Capacity {
  canSend: boolean;
  /** Why not, in words for the user. Null when there is nothing to say. */
  reason: string | null;
  /** True when only starting over will help, rather than a shorter message. */
  full: boolean;
}

const ROOM_TO_ANSWER =
  "This conversation has reached the size the API accepts. Start a new one to continue.";

/**
 * Whether `draft` can be added to `turns` and sent.
 *
 * Checks the same three limits the backend does - how many messages, how long
 * one is, and how large the whole conversation is - so a conversation that has
 * outgrown the endpoint says so instead of failing with a 422.
 */
export function describeCapacity(state: ConversationState, draft: string): Capacity {
  const content = draft.trim();

  if (state.status === "generating") {
    return { canSend: false, reason: null, full: false };
  }

  // A conversation already at the message limit cannot take another turn, with
  // or without a draft. Said up front so the composer can explain itself.
  if (state.turns.length >= MAX_MESSAGES) {
    return { canSend: false, reason: ROOM_TO_ANSWER, full: true };
  }

  if (usedCharacters(state.turns) >= MAX_TOTAL_CHARACTERS) {
    return { canSend: false, reason: ROOM_TO_ANSWER, full: true };
  }

  if (!content) {
    // Nothing typed yet is the ordinary state, not an error worth announcing.
    return { canSend: false, reason: null, full: false };
  }

  if (content.length > MAX_MESSAGE_CHARACTERS) {
    return {
      canSend: false,
      reason: `A single message can be at most ${MAX_MESSAGE_CHARACTERS.toLocaleString()} characters.`,
      full: false,
    };
  }

  if (usedCharacters(state.turns) + content.length > MAX_TOTAL_CHARACTERS) {
    return {
      canSend: false,
      reason: `That would take the conversation past ${MAX_TOTAL_CHARACTERS.toLocaleString()} characters. Shorten it, or start a new conversation.`,
      full: false,
    };
  }

  return { canSend: true, reason: null, full: false };
}

/** Characters the conversation already occupies, counted as the backend does. */
export function usedCharacters(turns: readonly Turn[]): number {
  return turns.reduce((total, turn) => total + turn.content.length, 0);
}

// -- Talking to the endpoint --------------------------------------------------

/**
 * Build the request body from the conversation.
 *
 * Only `messages`, and only the roles the user actually produced. Three
 * omissions, each deliberate:
 *
 * * **No `model`.** Nothing exposes which models the deployment allows, so the
 *   browser has no legitimate way to name one. Omitting it uses the configured
 *   model, which is the intended path.
 * * **No `system` message.** The schema has the role, but a system prompt the
 *   browser controls is a system prompt an end user controls. If the product
 *   needs one it belongs on the server.
 * * **No `temperature` or `max_output_tokens`.** The server's defaults are the
 *   policy; sending values would only let a client widen them.
 */
export function toGenerateRequest(turns: readonly Turn[]): GenerateRequest {
  const messages: AIMessage[] = turns.map((turn) => ({
    role: turn.role,
    content: turn.content,
  }));

  return { messages };
}
