/**
 * The AI workspace's conversation, as plain data and pure functions.
 *
 * No React and no network, for the same reason the session is: it can be tested
 * without a DOM, and the rules about what may be sent live in one readable place
 * rather than inside a component.
 *
 * **The conversation is the server's.** It is stored, tenant-scoped, and
 * addressed by `conversationId`; this module holds a *view* of it for the
 * length of a page visit. Two consequences shape everything below:
 *
 * * A send carries **one new user message**, plus the conversation it belongs
 *   to. The history is not re-sent, and a client could not rewrite it if it
 *   tried - the backend refuses anything but a single new user turn on an
 *   existing conversation.
 * * A run can come back **unfinished**. `awaiting_approval` means a person has
 *   been asked about a tool; the same run continues once they decide.
 *
 * Nothing is written to `localStorage`, `sessionStorage` or a cookie. What
 * survives a reload survives because the backend stored it, and reading it back
 * needs the session's token - which is in memory and nowhere else.
 */

import type {
  AgentRunRequest,
  AgentRunResponse,
  AIUsage,
  ConversationDetail,
  PendingApproval,
} from "@/types/ai";

/**
 * Limits mirrored from `app/schemas/agent.py`.
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

/**
 * A token that makes one send repeatable.
 *
 * Generated per user turn and kept on it, so retrying that turn sends the same
 * key and the backend hands back the run it already made instead of starting a
 * second one. A fresh value per turn, never derived from the message: two
 * identical questions are two questions.
 */
export function newIdempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return uuid;
  // A browser without `crypto.randomUUID` (an insecure context, an old engine)
  // still gets a key; uniqueness within one page is all that is required, since
  // the backend scopes keys per organization and a collision would only replay
  // this same user's own previous run.
  return `key-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export interface UserTurn {
  id: string;
  role: "user";
  content: string;
  /** Sent as `Idempotency-Key`, and reused when this turn is retried. */
  idempotencyKey?: string;
}

export interface AssistantTurn {
  id: string;
  role: "assistant";
  content: string;
  /**
   * Which tools the agent used to get there, in order.
   *
   * Names only. What it asked them and what they returned is the tenant's
   * business data and the agent's working; a reader gets the answer.
   */
  tools: string[];
  /** Absent on a turn read back from storage, which records no per-turn cost. */
  usage?: AIUsage;
  latencyMs?: number;
}

export type Turn = UserTurn | AssistantTurn;

/**
 * "awaiting_approval" is a state of the *conversation*, not a kind of error:
 * the run is paused, the composer stays shut, and what happens next is
 * somebody's decision rather than another message.
 */
export type ConversationStatus = "idle" | "generating" | "awaiting_approval";

export interface ConversationState {
  /** The stored conversation this view belongs to, once there is one. */
  conversationId: string | null;
  /** The most recent run, so it can be read back after a reload. */
  runId: string | null;
  turns: Turn[];
  status: ConversationStatus;
  /** What a paused run is waiting for. Null unless the status says otherwise. */
  approval: PendingApproval | null;
  /** The last failure, for the view to present. Cleared on the next attempt. */
  error: unknown;
}

export const EMPTY_CONVERSATION: ConversationState = {
  conversationId: null,
  runId: null,
  turns: [],
  status: "idle",
  approval: null,
  error: null,
};

export type ConversationAction =
  | { type: "send"; id: string; content: string; idempotencyKey: string }
  | { type: "retry" }
  | { type: "deciding" }
  | { type: "received"; id: string; response: AgentRunResponse }
  | { type: "failed"; error: unknown }
  | { type: "cancelled" }
  | { type: "loaded"; conversation: ConversationDetail; run: AgentRunResponse | null }
  | { type: "reset" };

/**
 * The conversation's only state transitions.
 *
 * Four rules are enforced here rather than in the view, because the view can be
 * raced by a fast second click and this cannot:
 *
 * * nothing starts while a generation is in flight, or while a decision is
 *   pending;
 * * a failure keeps every turn already in the conversation;
 * * a retry re-sends what is already there instead of adding the user's message
 *   a second time;
 * * a paused run records no assistant turn, because it has not answered.
 */
export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case "send": {
      const content = action.content.trim();
      // Both guards matter: an empty message is rejected by the backend, and a
      // second submission while the conversation is busy would interleave two
      // answers - or send a message into a run nobody has decided about yet.
      if (!content || state.status !== "idle") return state;

      return {
        ...state,
        turns: [
          ...state.turns,
          {
            id: action.id,
            role: "user",
            content,
            idempotencyKey: action.idempotencyKey,
          },
        ],
        status: "generating",
        error: null,
      };
    }

    case "retry": {
      // Only meaningful when the conversation is waiting on an answer that
      // never came, which is exactly when the last turn is the user's.
      if (state.status !== "idle" || !awaitingAnswer(state)) return state;
      return { ...state, status: "generating", error: null };
    }

    case "deciding": {
      // An approval is in flight. The agent will think once it lands, so this
      // is the same busy state a send produces.
      if (state.status !== "awaiting_approval") return state;
      return { ...state, status: "generating", error: null };
    }

    case "received": {
      if (state.status !== "generating") return state;
      return record(state, action.id, action.response);
    }

    case "failed":
      // The turns are untouched: a failed request must not cost the user the
      // conversation so far, nor the message they just typed.
      return { ...state, status: "idle", error: action.error };

    case "cancelled":
      // Stopping on purpose is not a failure, so no error is recorded and no
      // assistant turn is invented. A paused run is left paused - a person is
      // still being asked, whatever this page does.
      if (state.status === "awaiting_approval") return state;
      return { ...state, status: "idle", error: null };

    case "loaded":
      return hydrate(action.conversation, action.run);

    case "reset":
      return EMPTY_CONVERSATION;

    default:
      return state;
  }
}

/**
 * Fold a run's outcome into the conversation.
 *
 * Three shapes, and the middle one is the reason this is not a one-liner:
 *
 * * **completed** - the answer becomes an assistant turn;
 * * **awaiting_approval** - no turn at all, because nothing has been answered,
 *   and the approval is put where the view can show it;
 * * **anything else** - a run that ended without an answer. Recorded as an
 *   error rather than as silence, so the composer does not simply unlock with
 *   nothing having visibly happened.
 */
function record(
  state: ConversationState,
  id: string,
  response: AgentRunResponse,
): ConversationState {
  const base = {
    ...state,
    conversationId: response.conversation_id ?? state.conversationId,
    runId: response.run_id,
  };

  if (response.status === "awaiting_approval") {
    return { ...base, status: "awaiting_approval", approval: response.approval, error: null };
  }

  if (response.status === "completed") {
    return {
      ...base,
      status: "idle",
      approval: null,
      error: null,
      turns: [
        ...state.turns,
        {
          id,
          role: "assistant",
          // A completed run always carries an answer; the empty string is a
          // defensive floor rather than something the backend produces.
          content: response.final_response ?? "",
          tools: response.tool_calls.map((call) => call.tool_name),
          usage: response.usage,
          latencyMs: response.latency_ms,
        },
      ],
    };
  }

  return {
    ...base,
    status: "idle",
    approval: null,
    error: new Error(unfinishedReason(response)),
  };
}

/** Why a run has no answer yet, in words a user can act on. */
function unfinishedReason(response: AgentRunResponse): string {
  if (response.status === "pending" || response.status === "running") {
    // Reached by replaying an idempotency key while the original request is
    // still being served - two tabs, or a very fast retry. Nothing has gone
    // wrong, and starting a second run is exactly what the key prevents.
    return "That message is still being worked on. Give it a moment and reload the page.";
  }
  if (response.error_code === "agent_run_abandoned") {
    return "That run stopped before it finished and could not be safely resumed. Ask again.";
  }
  if (response.status === "cancelled") {
    return "That run was cancelled before it produced an answer.";
  }
  return "The agent stopped without producing an answer. Try asking again.";
}

/**
 * Rebuild the view from what the backend stored.
 *
 * Tool turns are folded into the answer that followed them, which is where a
 * reader expects to see "this is how it knew that". They carry a name and
 * nothing else - the arguments and the returned records are not in the payload
 * to begin with.
 */
function hydrate(
  conversation: ConversationDetail,
  run: AgentRunResponse | null,
): ConversationState {
  const turns: Turn[] = [];
  let tools: string[] = [];

  for (const turn of conversation.turns) {
    if (turn.role === "tool_request" || turn.role === "tool_result") {
      if (turn.tool_name && !tools.includes(turn.tool_name)) tools.push(turn.tool_name);
      continue;
    }

    if (turn.role === "user") {
      turns.push({ id: turn.id, role: "user", content: turn.content ?? "" });
      continue;
    }

    turns.push({ id: turn.id, role: "assistant", content: turn.content ?? "", tools });
    tools = [];
  }

  const paused = run?.status === "awaiting_approval";

  return {
    conversationId: conversation.id,
    runId: run?.run_id ?? null,
    turns,
    status: paused ? "awaiting_approval" : "idle",
    approval: paused ? run.approval : null,
    error: null,
  };
}

/** Whether the last thing said was the user's, with no answer yet. */
export function awaitingAnswer(state: ConversationState): boolean {
  return state.turns.at(-1)?.role === "user";
}

/** Whether a failed or cancelled attempt can be tried again. */
export function canRetry(state: ConversationState): boolean {
  return state.status === "idle" && awaitingAnswer(state);
}

/** Whether a person still has to decide something before this can go on. */
export function isAwaitingApproval(state: ConversationState): boolean {
  return state.status === "awaiting_approval" && state.approval !== null;
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

const WAITING_ON_SOMEBODY =
  "This conversation is waiting for somebody to approve an action before it can go on.";

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

  if (state.status === "awaiting_approval") {
    return { canSend: false, reason: WAITING_ON_SOMEBODY, full: false };
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
 * **One message, and which conversation it belongs to.** The history is the
 * server's: re-sending it would be a client asserting what was already said,
 * and the backend refuses anything but a single new user turn on a conversation
 * that already exists.
 *
 * Three further omissions, each deliberate:
 *
 * * **No `model`.** An agent's model is part of its server-side configuration,
 *   and the request schema has no field for one.
 * * **No `system` message.** A system prompt the browser controls is a system
 *   prompt an end user controls. If the product needs one it belongs on the
 *   server.
 * * **No `temperature` or `max_output_tokens`.** The server's defaults are the
 *   policy; sending values would only let a client widen them.
 *
 * Returns null when there is nothing to send, which is every state except "the
 * user has said something nobody has answered".
 */
export function toAgentRunRequest(state: ConversationState): AgentRunRequest | null {
  const last = state.turns.at(-1);
  if (last === undefined || last.role !== "user") return null;

  const body: AgentRunRequest = { messages: [{ role: "user", content: last.content }] };
  if (state.conversationId !== null) body.conversation_id = state.conversationId;

  return body;
}

/** The key to send with the current turn, if it has one. */
export function pendingIdempotencyKey(state: ConversationState): string | undefined {
  const last = state.turns.at(-1);
  return last?.role === "user" ? last.idempotencyKey : undefined;
}
