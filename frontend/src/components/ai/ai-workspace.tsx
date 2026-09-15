"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";

import { ApprovalPanel } from "@/components/ai/approval-panel";
import { ConversationView } from "@/components/ai/conversation-view";
import { MessageComposer } from "@/components/ai/message-composer";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import {
  approveAction,
  getConversation,
  getRun,
  isAbortError,
  listAgents,
  listApprovals,
  listConversations,
  rejectAction,
  runAgent,
} from "@/lib/api";
import { isAdministrator } from "@/lib/auth/permissions";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import {
  canRetry,
  conversationReducer,
  describeCapacity,
  EMPTY_CONVERSATION,
  isAwaitingApproval,
  newIdempotencyKey,
  nextTurnId,
  pendingIdempotencyKey,
  toAgentRunRequest,
  type ConversationState,
} from "@/lib/ai/conversation";
import type { AgentRunResponse, ConversationDetail } from "@/types/ai";

/**
 * The AI workspace.
 *
 * Keyed by the acting organization, so switching tenant starts from that
 * organization's own stored conversations rather than leaving one tenant's
 * transcript on screen while the next message is attributed to another.
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

  return (
    <AgentWorkspace key={active.organization.id} canDecide={isAdministrator(active.role)} />
  );
}

/**
 * Picks the agent and the conversation, then runs it.
 *
 * Both are fetched once per tenant rather than once per message. The
 * conversation is the *stored* one: the workspace asks the backend what this
 * organization was last talking about, so closing the page and coming back
 * resumes rather than restarts. Nothing is kept in browser storage - what
 * survives a reload survives because the server has it, and reading it back
 * needs the in-memory token.
 */
function AgentWorkspace({ canDecide }: { canDecide: boolean }) {
  const api = useApi();

  const workspace = useAsyncResource((signal) => loadWorkspace(api, signal));

  if (workspace.state.kind === "loading") {
    return <LoadingState label="Preparing the workspace ..." />;
  }
  if (workspace.state.kind === "error") {
    return <ErrorState error={workspace.state.error} onRetry={workspace.reload} />;
  }

  const { agent, initial } = workspace.state.data;

  if (agent === undefined) {
    return (
      <EmptyState title="No agent is available">
        This organization has no agent configured to answer questions.
      </EmptyState>
    );
  }

  return <Conversation agentId={agent.id} initial={initial} canDecide={canDecide} />;
}

interface Workspace {
  agent: { id: string } | undefined;
  initial: ConversationState;
}

/**
 * What the page needs before it can show anything.
 *
 * The most recent conversation and, if its last run is paused, that run - so a
 * page reopened while somebody was still deciding comes back showing exactly
 * that, rather than an empty composer.
 */
async function loadWorkspace(
  api: ReturnType<typeof useApi>,
  signal: AbortSignal,
): Promise<Workspace> {
  const [agents, conversations] = await Promise.all([
    listAgents(api, { signal }),
    listConversations(api, { signal }),
  ]);

  const latest = conversations[0];
  if (latest === undefined) {
    return { agent: agents[0], initial: EMPTY_CONVERSATION };
  }

  const detail = await getConversation(api, latest.id, { signal });
  const run = await pausedRunOf(api, detail, signal);

  return {
    agent: agents[0],
    initial: conversationReducer(EMPTY_CONVERSATION, {
      type: "loaded",
      conversation: detail,
      run,
    }),
  };
}

/**
 * The run a stored conversation is waiting on, if it is waiting on one.
 *
 * Found through the approval queue rather than guessed from the transcript: the
 * queue is the authoritative list of what is pending, it is already scoped to
 * this organization, and any active member may read it. A conversation with
 * nothing pending needs no second request.
 */
async function pausedRunOf(
  api: ReturnType<typeof useApi>,
  conversation: ConversationDetail,
  signal: AbortSignal,
): Promise<AgentRunResponse | null> {
  const last = conversation.turns.at(-1);
  // A paused conversation ends on the request that paused it. Anything else has
  // been answered, so there is nothing to look up.
  if (last === undefined || last.role !== "tool_request") return null;

  const { approvals } = await listApprovals(api, { signal });
  const mine = approvals.find((approval) => approval.conversation_id === conversation.id);
  if (mine?.run_id == null) return null;

  return getRun(api, mine.run_id, { signal });
}

/**
 * One conversation.
 *
 * State is a local `useReducer` - no store - because nothing outside this screen
 * needs it, and what has to outlive the page is in the database rather than
 * here.
 *
 * The request goes through the shared API client to the platform's own agent
 * endpoint. Which provider serves it, whether it was retried, and what
 * credentials it used are decided on the server; none of that is knowable from
 * here, which is the point of the gateway sitting behind the endpoint.
 */
function Conversation({
  agentId,
  initial,
  canDecide,
}: {
  agentId: string;
  initial: ConversationState;
  canDecide: boolean;
}) {
  const api = useApi();
  const [conversation, dispatch] = useReducer(conversationReducer, initial);

  /** The in-flight request, so it can be cancelled or abandoned on unmount. */
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => {
    // Leaving the page should not leave a generation running.
    return () => inFlight.current?.abort();
  }, []);

  /**
   * Run whatever the conversation is currently asking, and record the outcome.
   *
   * Called from event handlers rather than an effect: an effect would be invoked
   * twice under React's development double-render, and this request costs money
   * to make.
   *
   * Every outcome is guarded by `inFlight.current === controller`. Only the
   * current attempt may write to the conversation, so a reply that arrives after
   * its request was cancelled or superseded is discarded instead of landing in a
   * conversation that has moved on.
   */
  const dispatchRun = useCallback(
    async (call: (signal: AbortSignal) => Promise<AgentRunResponse>): Promise<void> => {
      const controller = new AbortController();
      inFlight.current = controller;

      const current = (): boolean => inFlight.current === controller;

      try {
        const response = await call(controller.signal);
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
    [],
  );

  const start = useCallback(
    (state: ConversationState): void => {
      const request = toAgentRunRequest(state);
      if (request === null) return;

      const idempotencyKey = pendingIdempotencyKey(state);

      void dispatchRun((signal) => runAgent(api, agentId, request, { signal, idempotencyKey }));
    },
    [api, agentId, dispatchRun],
  );

  const send = useCallback(
    (content: string): void => {
      const text = content.trim();
      // The same check the reducer makes. Here too, so nothing is sent when the
      // reducer would refuse to record it.
      if (!describeCapacity(conversation, text).canSend) return;

      const action = {
        type: "send" as const,
        id: nextTurnId(),
        content: text,
        idempotencyKey: newIdempotencyKey(),
      };

      dispatch(action);
      // Built from the state the reducer will produce, so the key and the turn
      // that carries it are the same ones.
      start(conversationReducer(conversation, action));
    },
    [conversation, start],
  );

  const retry = useCallback((): void => {
    if (!canRetry(conversation)) return;
    dispatch({ type: "retry" });
    // The user's message is already in the conversation, and it keeps the key
    // it was first sent with - which is what stops a retry asking twice.
    start(conversation);
  }, [conversation, start]);

  const decide = useCallback(
    (approve: boolean, reason: string): void => {
      const approval = conversation.approval;
      if (approval === null || conversation.status !== "awaiting_approval") return;

      dispatch({ type: "deciding" });
      // A decision says what it resumed. For this screen that is always the
      // agent run - a workflow's approvals are decided on the workflows page.
      void dispatchRun(async (signal) => {
        const decision = approve
          ? await approveAction(api, approval.id, { signal, reason })
          : await rejectAction(api, approval.id, { signal, reason });

        if (decision.agent_run === null) {
          throw new Error("That approval belongs to a workflow, not this conversation.");
        }
        return decision.agent_run;
      });
    },
    [api, conversation, dispatchRun],
  );

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
  const paused = isAwaitingApproval(conversation);

  return (
    <div className="flex min-h-[60vh] flex-col">
      <div className="flex-1">
        <ConversationView turns={conversation.turns} generating={generating} />
      </div>

      {paused && conversation.approval !== null && (
        <ApprovalPanel
          approval={conversation.approval}
          canDecide={canDecide}
          deciding={generating}
          error={conversation.error}
          onApprove={(reason) => decide(true, reason)}
          onReject={(reason) => decide(false, reason)}
        />
      )}

      {conversation.error !== null && !paused && (
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
            disabled={generating || paused}
          >
            New conversation
          </Button>
        </div>
      )}
    </div>
  );
}
