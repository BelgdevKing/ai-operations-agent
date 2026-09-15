/**
 * How an execution state is described to an operator.
 *
 * Plain data and pure functions, no React and no network - so the vocabulary
 * can be tested without a DOM, and so there is one place to read when somebody
 * asks "what does the console mean by *failed*?".
 *
 * **The vocabulary is the backend's.** `AgentRunStatus` and `ToolOutcome` come
 * from `types/ai.ts`, which mirrors `app/agents/models.py` and
 * `app/tools/models.py`. Nothing here invents a status, widens one, or collapses
 * two into one: the maps below are total over the union, so a status added to
 * the backend is a TypeScript error here rather than a blank badge in
 * production.
 *
 * **No colour-only signalling.** Every state has a word as well as a tone, and
 * the word is what a screen reader and a monochrome display get.
 */

import type { BadgeTone } from "@/components/ui/badge";
import type { AgentRunStatus, ToolCallSummary, ToolOutcome } from "@/types/ai";

/** What each run state is called, in an operator's words rather than the enum's. */
export const RUN_LABELS: Record<AgentRunStatus, string> = {
  pending: "Queued",
  running: "Running",
  awaiting_approval: "Waiting for approval",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const RUN_TONES: Record<AgentRunStatus, BadgeTone> = {
  pending: "neutral",
  running: "accent",
  awaiting_approval: "warn",
  completed: "ok",
  failed: "danger",
  cancelled: "neutral",
};

/**
 * States in which the run is still going to change on its own.
 *
 * `awaiting_approval` is deliberately **not** one of them: it changes when a
 * person decides, which is an action rather than the passage of time, and
 * treating it as in-flight is what would justify polling it.
 */
const IN_FLIGHT: ReadonlySet<AgentRunStatus> = new Set<AgentRunStatus>(["pending", "running"]);

export function isInFlight(status: AgentRunStatus): boolean {
  return IN_FLIGHT.has(status);
}

/** States a run never leaves. */
const TERMINAL: ReadonlySet<AgentRunStatus> = new Set<AgentRunStatus>([
  "completed",
  "failed",
  "cancelled",
]);

export function isTerminal(status: AgentRunStatus): boolean {
  return TERMINAL.has(status);
}

/**
 * Whether the console may offer to stop this run.
 *
 * Only `awaiting_approval`, which is exactly what `POST /ai/runs/{id}/cancel`
 * accepts - the backend refuses anything else with a 409, and an interface that
 * offered a button for it would be promising something the server has already
 * decided not to do. A run that is *running* is being advanced by a request
 * right now; a finished one has nothing to stop.
 */
export function isCancellable(status: AgentRunStatus): boolean {
  return status === "awaiting_approval";
}

/** What each tool outcome is called. */
export const TOOL_LABELS: Record<ToolOutcome, string> = {
  succeeded: "succeeded",
  failed: "failed",
  timed_out: "timed out",
  cancelled: "cancelled",
  approval_required: "waiting for approval",
  rejected: "declined",
};

export const TOOL_TONES: Record<ToolOutcome, BadgeTone> = {
  succeeded: "ok",
  failed: "danger",
  timed_out: "danger",
  cancelled: "neutral",
  approval_required: "warn",
  rejected: "neutral",
};

/**
 * A mark for a tool's outcome, for the text-only column beside the badge.
 *
 * Characters rather than icons, so the state survives a monochrome display and
 * a copy-paste into a ticket. Never the only signal: the label is always there.
 */
export const TOOL_MARKS: Record<ToolOutcome, string> = {
  succeeded: "✓",
  failed: "×",
  timed_out: "×",
  cancelled: "–",
  approval_required: "…",
  rejected: "–",
};

/** How a tool call reads when the backend has not recorded an outcome yet. */
export const TOOL_PENDING = { label: "running", tone: "accent" as BadgeTone, mark: "…" };

export interface ToolPresentation {
  label: string;
  tone: BadgeTone;
  mark: string;
}

export function describeTool(call: ToolCallSummary): ToolPresentation {
  if (call.outcome === null) return { ...TOOL_PENDING };
  return {
    label: TOOL_LABELS[call.outcome],
    tone: TOOL_TONES[call.outcome],
    mark: TOOL_MARKS[call.outcome],
  };
}

/**
 * A duration in the units somebody reading a console actually wants.
 *
 * Milliseconds below a second, because a fast tool call is interesting at that
 * resolution; seconds above it, because nobody needs three decimal places on a
 * run that took a minute.
 */
export function describeDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "–";
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;

  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.round((milliseconds % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

/** `agent_run_abandoned` -> `Agent run abandoned`. */
export function describeErrorCode(code: string): string {
  const words = code.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
