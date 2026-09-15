"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { ApprovalPanel } from "@/components/ai/approval-panel";
import { ConversationView } from "@/components/ai/conversation-view";
import { MessageComposer } from "@/components/ai/message-composer";
import { AgentPicker } from "@/components/console/agent-picker";
import { ConversationPicker } from "@/components/console/conversation-picker";
import { ExecutionPanel } from "@/components/console/execution-panel";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import {
  approveAction,
  cancelRun,
  getConversation,
  getRun,
  isAbortError,
  listAgents,
  listApprovals,
  listConversations,
  rejectAction,
  runAgent,
} from "@/lib/api";
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
import { isAdministrator } from "@/lib/auth/permissions";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import type {
  AgentRunResponse,
  AgentSummary,
  ConversationDetail,
  ConversationSummary,
} from "@/types/ai";

/**
 * The agent console.
 *
 * Where an operations team supervises the platform's agents: pick one, open a
 * stored conversation or start a new one, ask for something, and watch what the
 * agent did about it - which tools it reached for, what it is waiting on, and
 * how the run ended.
 *
 * **Everything on screen is the server's.** The agent list, the conversations,
 * the transcript, the run and the approval all come from tenant-scoped
 * endpoints and are re-read rather than reconstructed. Nothing is written to
 * `localStorage`, `sessionStorage`, `indexedDB` or a cookie: what survives a
 * reload survives because the backend stored it, and reading it back needs the
 * in-memory token.
 *
 * **Selecting is not authorizing.** Choosing an agent only changes which id the
 * next run is addressed to. The backend resolves it against the same verified
 * membership every time and answers 404 for one belonging to another
 * organization - so this console has no organization control, and could not
 * usefully have one.
 *
 * Keyed by the acting organization, so switching tenant starts from that
 * organization's own agents and conversations rather than leaving one tenant's
 * transcript on screen while the next request is attributed to another.
 */
export function AiWorkspace() {
  const { active } = useAuthenticatedSession();

  if (active === null) {
    return (
      <ErrorState
        error={
          new Error("You are not acting as an active organization, so the console is unavailable.")
        }
      />
    );
  }

  return <Console key={active.organization.id} canDecide={isAdministrator(active.role)} />;
}

interface ConsoleData {
  agents: AgentSummary[];
  conversations: ConversationSummary[];
}

/**
 * What the console needs before it can show anything: who it may talk to, and
 * what it has talked about. Both once per tenant rather than once per message.
 */
async function loadConsole(
  api: ReturnType<typeof useApi>,
  signal: AbortSignal,
): Promise<ConsoleData> {
  const [agents, conversations] = await Promise.all([
    listAgents(api, { signal }),
    listConversations(api, { signal }),
  ]);
  return { agents, conversations };
}

function Console({ canDecide }: { canDecide: boolean }) {
  const api = useApi();
  const workspace = useAsyncResource((signal) => loadConsole(api, signal));

  const [agentId, setAgentId] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);

  if (workspace.state.kind === "loading") {
    return <LoadingState label="Loading the console ..." />;
  }
  if (workspace.state.kind === "error") {
    return <ErrorState error={workspace.state.error} onRetry={workspace.reload} />;
  }

  const { agents, conversations } = workspace.state.data;

  if (agents.length === 0) {
    return (
      <EmptyState title="No agent is available">
        This organization has no agent configured to answer questions. An agent has to exist
        before the console has anything to supervise.
      </EmptyState>
    );
  }

  // The first agent is the default rather than the only one: an operator who
  // has not chosen yet should still be able to ask something.
  const selectedAgent = agents.find((agent) => agent.id === agentId) ?? agents[0]!;

  return (
    <Session
      // Remounting on either selection is what keeps the transcript honest: a
      // conversation belongs to one agent and one thread, and carrying reducer
      // state across a change would show one conversation's turns under
      // another's heading.
      key={`${selectedAgent.id}:${conversationId ?? "new"}`}
      agents={agents}
      conversations={conversations}
      agent={selectedAgent}
      conversationId={conversationId}
      canDecide={canDecide}
      onSelectAgent={setAgentId}
      onSelectConversation={setConversationId}
      onConversationsChanged={workspace.reload}
    />
  );
}

interface SessionProps {
  agents: AgentSummary[];
  conversations: ConversationSummary[];
  agent: AgentSummary;
  conversationId: string | null;
  canDecide: boolean;
  onSelectAgent: (agentId: string) => void;
  onSelectConversation: (conversationId: string | null) => void;
  onConversationsChanged: () => void;
}

/** One agent and one conversation, loaded before anything is rendered. */
function Session(props: SessionProps) {
  const api = useApi();
  const { conversationId } = props;

  const stored = useAsyncResource((signal) =>
    conversationId === null
      ? Promise.resolve(null)
      : openConversation(api, conversationId, signal),
  );

  if (stored.state.kind === "loading") return <LoadingState label="Loading the conversation ..." />;
  if (stored.state.kind === "error") {
    return <ErrorState error={stored.state.error} onRetry={stored.reload} />;
  }

  const opened = stored.state.data;

  return (
    <Transcript
      {...props}
      initial={opened?.state ?? EMPTY_CONVERSATION}
      initialRun={opened?.run ?? null}
    />
  );
}

interface OpenedConversation {
  state: ConversationState;
  run: AgentRunResponse | null;
}

/**
 * A stored conversation, and the run it is waiting on if it is waiting on one.
 *
 * The paused run is found through the approval queue rather than guessed from
 * the transcript: the queue is the authoritative list of what is pending, it is
 * already scoped to this organization, and any active member may read it. A
 * conversation with nothing pending needs no second request.
 */
async function openConversation(
  api: ReturnType<typeof useApi>,
  conversationId: string,
  signal: AbortSignal,
): Promise<OpenedConversation> {
  const detail = await getConversation(api, conversationId, { signal });
  const run = await pausedRunOf(api, detail, signal);

  return {
    state: conversationReducer(EMPTY_CONVERSATION, { type: "loaded", conversation: detail, run }),
    run,
  };
}

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
 * One conversation, and the run in front of it.
 *
 * State is a local `useReducer` - no store - because nothing outside this
 * screen needs it, and what has to outlive the page is in the database. The
 * *run* is held beside it in plain state, set from the very same
 * `AgentRunResponse` the reducer is given, so the two cannot disagree about
 * what happened.
 */
function Transcript({
  agents,
  conversations,
  agent,
  conversationId,
  canDecide,
  initial,
  initialRun,
  onSelectAgent,
  onSelectConversation,
  onConversationsChanged,
}: SessionProps & { initial: ConversationState; initialRun: AgentRunResponse | null }) {
  const api = useApi();
  const [conversation, dispatch] = useReducer(conversationReducer, initial);
  const [run, setRun] = useState<AgentRunResponse | null>(initialRun);
  const [reconciling, setReconciling] = useState(false);

  /** The in-flight request, so it can be abandoned on unmount or superseded. */
  const inFlight = useRef<AbortController | null>(null);

  /** The conversation the sidebar already knows about, so it is asked once. */
  const knownConversation = useRef<string | null>(initial.conversationId);

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
   * current attempt may write, so a reply that arrives after its request was
   * superseded is discarded instead of landing in a conversation that has moved
   * on - which is the whole defence against a stale response overwriting a
   * newer one.
   */
  const dispatchRun = useCallback(
    async (call: (signal: AbortSignal) => Promise<AgentRunResponse>): Promise<void> => {
      const controller = new AbortController();
      inFlight.current = controller;

      const current = (): boolean => inFlight.current === controller;

      try {
        const response = await call(controller.signal);
        if (!current()) return;
        // Both from the same response object, so the transcript and the
        // execution figures can never describe different runs.
        dispatch({ type: "received", id: nextTurnId(), response });
        setRun(response);

        // The first run of a new thread is what brings the conversation into
        // existence, so the sidebar's list is a request behind until it is
        // asked again. `reload` re-fetches without dropping back to a loading
        // state, so the transcript on screen is undisturbed.
        if (response.conversation_id && response.conversation_id !== knownConversation.current) {
          knownConversation.current = response.conversation_id;
          onConversationsChanged();
        }
      } catch (error) {
        if (!current()) return;
        // Stopping on purpose is not a failure and gets no error banner.
        if (isAbortError(error)) dispatch({ type: "cancelled" });
        else dispatch({ type: "failed", error });
      } finally {
        if (current()) inFlight.current = null;
      }
    },
    [onConversationsChanged],
  );

  const start = useCallback(
    (state: ConversationState): void => {
      const request = toAgentRunRequest(state);
      if (request === null) return;

      const idempotencyKey = pendingIdempotencyKey(state);

      void dispatchRun((signal) => runAgent(api, agent.id, request, { signal, idempotencyKey }));
    },
    [api, agent.id, dispatchRun],
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
      void dispatchRun(async (signal) => {
        const decision = approve
          ? await approveAction(api, approval.id, { signal, reason })
          : await rejectAction(api, approval.id, { signal, reason });

        // A decision says what it resumed. On this screen that is always the
        // agent run; an approval that resumed a workflow instead belongs to the
        // workflows console, and guessing would put one process's outcome under
        // another's transcript.
        if (decision.agent_run === null) {
          throw new Error("That approval belongs to a workflow. Decide it on the Workflows page.");
        }
        return decision.agent_run;
      });
    },
    [api, conversation, dispatchRun],
  );

  /**
   * Take an authoritative run and rebuild the view around it.
   *
   * Shared by refreshing and by stopping, so the two cannot drift about what a
   * moved-on run looks like. The transcript is re-read from the server rather
   * than patched: the server is authoritative about what was said, and
   * appending a turn locally is how a conversation ends up carrying the same
   * answer twice. `hydrate` then derives the paused state from the run itself,
   * so an approval that no longer applies disappears with it.
   *
   * A run with no conversation cannot be rebuilt - there is nothing to re-read -
   * so its figures are updated and the transcript is left alone. A paused agent
   * run always has one, since the approval was found through it.
   */
  const rebuildFrom = useCallback(
    async (latest: AgentRunResponse): Promise<void> => {
      setRun(latest);
      if (!latest.conversation_id) return;

      const detail = await getConversation(api, latest.conversation_id);
      dispatch({ type: "loaded", conversation: detail, run: latest });
    },
    [api],
  );

  /**
   * Re-read the run, and the conversation with it when the run has moved on.
   *
   * The platform has no push or stream - a run is advanced inside the request
   * that asked for it - so reconciling is a deliberate act rather than a timer.
   * A run waiting on a person changes when that person decides, which may be
   * hours away; polling for it would be load nobody asked for.
   *
   * When a paused run turns out to have been decided elsewhere, the transcript
   * is reloaded from the server rather than patched here: the server is
   * authoritative about what was said, and appending a turn locally is how a
   * conversation ends up carrying the same answer twice.
   */
  const reconcile = useCallback(async (): Promise<void> => {
    const runId = run?.run_id ?? conversation.runId;
    if (!runId) return;

    setReconciling(true);
    try {
      await rebuildFrom(await getRun(api, runId));
    } catch (error) {
      dispatch({ type: "failed", error });
    } finally {
      setReconciling(false);
    }
  }, [api, conversation.runId, rebuildFrom, run]);

  /**
   * Stop a run that is waiting on somebody.
   *
   * Only offered where the backend accepts it, and re-checked by the backend
   * regardless - a run already decided answers 409, which is shown rather than
   * swallowed.
   *
   * The view is then rebuilt from the server rather than nudged locally. The
   * reducer's `cancelled` action is deliberately a no-op on a paused
   * conversation - it exists for the client abandoning a *generation*, and
   * leaving a paused run paused is exactly right for that - but this
   * cancellation is durable: the server has ended the run and withdrawn the
   * approval. Dispatching `cancelled` here would leave an approve button on
   * screen for a run that no longer exists to approve.
   */
  const stop = useCallback(async (): Promise<void> => {
    const runId = run?.run_id ?? conversation.runId;
    if (!runId) return;

    setReconciling(true);
    try {
      await rebuildFrom(await cancelRun(api, runId));
    } catch (error) {
      dispatch({ type: "failed", error });
    } finally {
      setReconciling(false);
    }
  }, [api, conversation.runId, rebuildFrom, run]);

  const abandon = useCallback((): void => {
    inFlight.current?.abort();
    inFlight.current = null;
  }, []);

  const startNew = useCallback((): void => {
    inFlight.current?.abort();
    inFlight.current = null;
    onSelectConversation(null);
    onConversationsChanged();
  }, [onConversationsChanged, onSelectConversation]);

  const capacityOf = useCallback(
    (draft: string) => describeCapacity(conversation, draft),
    [conversation],
  );

  const generating = conversation.status === "generating";
  const paused = isAwaitingApproval(conversation);
  const busy = generating || reconciling;
  const full = describeCapacity(conversation, "").full;

  return (
    <div className="grid gap-4 lg:grid-cols-[17rem_1fr] lg:items-start">
      {/* The sidebar comes first in the DOM so tab order runs "what am I
          talking to" before "what am I saying", and it stacks above the
          transcript on a narrow viewport for the same reason. */}
      <aside aria-label="Console" className="space-y-4">
        <section aria-labelledby="console-agents" className="space-y-2">
          <h2
            id="console-agents"
            className="text-xs font-semibold tracking-wide text-ink-muted uppercase"
          >
            Agents
          </h2>
          <AgentPicker
            agents={agents}
            selectedId={agent.id}
            disabled={busy}
            onSelect={onSelectAgent}
          />
        </section>

        <section aria-labelledby="console-conversations" className="space-y-2">
          <h2
            id="console-conversations"
            className="text-xs font-semibold tracking-wide text-ink-muted uppercase"
          >
            Conversations
          </h2>
          <ConversationPicker
            conversations={conversations}
            selectedId={conversationId}
            disabled={busy}
            onSelect={onSelectConversation}
            onStartNew={startNew}
          />
        </section>
      </aside>

      <div className="flex min-h-[60vh] flex-col">
        <div className="flex-1">
          <ConversationView turns={conversation.turns} generating={generating} />
        </div>

        {paused && conversation.approval !== null && (
          <ApprovalPanel
            approval={conversation.approval}
            canDecide={canDecide}
            deciding={busy}
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

        {run !== null && (
          <div className="mt-4">
            <ExecutionPanel
              run={run}
              busy={busy}
              onRefresh={() => void reconcile()}
              onCancel={() => void stop()}
            />
          </div>
        )}

        {full && (
          <div className="mt-4 rounded-lg border border-warn/30 bg-warn/10 px-4 py-3 text-sm">
            <p className="font-medium text-ink">This conversation is full</p>
            <p className="mt-1 text-ink-muted">
              It has reached the size the API accepts. Start a new one to keep going - the
              messages above stay until you do.
            </p>
            <Button variant="secondary" className="mt-3" onClick={startNew}>
              Start new conversation
            </Button>
          </div>
        )}

        <MessageComposer
          generating={generating}
          capacityOf={capacityOf}
          onSend={send}
          onCancel={abandon}
        />
      </div>
    </div>
  );
}
