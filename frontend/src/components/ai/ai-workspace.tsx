"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";

import { ConversationView } from "@/components/ai/conversation-view";
import { MessageComposer } from "@/components/ai/message-composer";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/states";
import { generate, isAbortError } from "@/lib/api";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import {
  canRetry,
  conversationReducer,
  describeCapacity,
  EMPTY_CONVERSATION,
  nextTurnId,
  toGenerateRequest,
  type Turn,
} from "@/lib/ai/conversation";

/**
 * The AI workspace.
 *
 * Keyed by the acting organization, so switching tenant starts a fresh
 * conversation rather than leaving answers generated for one organization on
 * screen while the next message is attributed to another.
 */
export function AiWorkspace() {
  const { active } = useAuthenticatedSession();

  if (active === null) {
    return (
      <ErrorState
        error={
          new Error("You are not acting as an active organization, so generation is unavailable.")
        }
      />
    );
  }

  return <Conversation key={active.organization.id} />;
}

/**
 * One conversation.
 *
 * State is a local `useReducer` - no store - because nothing outside this
 * screen needs it and it is gone on reload either way.
 *
 * The request goes through the shared API client to the platform's own AI
 * endpoint. Which provider serves it, whether it was retried, and what
 * credentials it used are decided on the server; none of that is knowable from
 * here, which is the point of the gateway sitting behind the endpoint.
 */
function Conversation() {
  const api = useApi();
  const [conversation, dispatch] = useReducer(conversationReducer, EMPTY_CONVERSATION);

  /** The in-flight request, so it can be cancelled or abandoned on unmount. */
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => {
    // Leaving the page should not leave a generation running.
    return () => inFlight.current?.abort();
  }, []);

  /**
   * Send `turns` and record whatever comes back.
   *
   * Called from the event handler rather than an effect: an effect would be
   * invoked twice under React's development double-render, and this request
   * costs money to make.
   *
   * Every outcome is guarded by `inFlight.current === controller`. Only the
   * current attempt may write to the conversation, so a reply that arrives
   * after its request was cancelled or superseded is discarded instead of
   * landing in a conversation that has moved on.
   */
  const run = useCallback(
    async (turns: readonly Turn[]): Promise<void> => {
      const controller = new AbortController();
      inFlight.current = controller;

      const current = (): boolean => inFlight.current === controller;

      try {
        const response = await generate(api, toGenerateRequest(turns), {
          signal: controller.signal,
        });
        if (current()) dispatch({ type: "received", id: nextTurnId(), response });
      } catch (error) {
        if (!current()) return;
        // Stopping on purpose is not a failure and gets no error banner.
        if (isAbortError(error)) dispatch({ type: "cancelled" });
        else dispatch({ type: "failed", error });
      } finally {
        if (current()) inFlight.current = null;
      }
    },
    [api],
  );

  const send = useCallback(
    (content: string): void => {
      const text = content.trim();
      // The same check the reducer makes. Here too, so nothing is sent when the
      // reducer would refuse to record it.
      if (!describeCapacity(conversation, text).canSend) return;

      const turn: Turn = { id: nextTurnId(), role: "user", content: text };
      dispatch({ type: "send", id: turn.id, content: text });
      void run([...conversation.turns, turn]);
    },
    [conversation, run],
  );

  const retry = useCallback((): void => {
    if (!canRetry(conversation)) return;
    dispatch({ type: "retry" });
    // The user's message is already in the conversation; re-sending it as it
    // stands is what keeps a retry from asking the same question twice.
    void run(conversation.turns);
  }, [conversation, run]);

  const cancel = useCallback((): void => {
    inFlight.current?.abort();
    inFlight.current = null;
  }, []);

  const reset = useCallback((): void => {
    inFlight.current?.abort();
    inFlight.current = null;
    dispatch({ type: "reset" });
  }, []);

  const capacityOf = useCallback(
    (draft: string) => describeCapacity(conversation, draft),
    [conversation],
  );

  const generating = conversation.status === "generating";
  const full = describeCapacity(conversation, "").full;

  return (
    <div className="flex min-h-[60vh] flex-col">
      <div className="flex-1">
        <ConversationView turns={conversation.turns} generating={generating} />
      </div>

      {conversation.error !== null && (
        <ErrorState
          className="mt-4"
          error={conversation.error}
          onRetry={canRetry(conversation) ? retry : undefined}
        />
      )}

      {full && (
        <div className="mt-4 rounded-lg border border-warn/30 bg-warn/10 px-4 py-3 text-sm">
          <p className="font-medium text-ink">This conversation is full</p>
          <p className="mt-1 text-ink-muted">
            It has reached the size the API accepts. Start a new one to keep going - the messages
            above stay until you do.
          </p>
          <Button variant="secondary" className="mt-3" onClick={reset}>
            Start new conversation
          </Button>
        </div>
      )}

      <MessageComposer
        generating={generating}
        capacityOf={capacityOf}
        onSend={send}
        onCancel={cancel}
      />

      {conversation.turns.length > 0 && !full && (
        <div className="mt-3">
          <Button
            variant="ghost"
            className="px-2 py-1 text-xs"
            onClick={reset}
            disabled={generating}
          >
            New conversation
          </Button>
        </div>
      )}
    </div>
  );
}
