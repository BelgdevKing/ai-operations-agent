"use client";

import { ToolActivity } from "@/components/console/tool-activity";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/states";
import {
  RUN_LABELS,
  RUN_TONES,
  describeDuration,
  describeErrorCode,
  isCancellable,
} from "@/lib/ai/run-status";
import type { AgentRunResponse } from "@/types/ai";

export interface ExecutionPanelProps {
  run: AgentRunResponse;
  /** True while a refresh, a cancellation or a decision is in flight. */
  busy: boolean;
  onRefresh: () => void;
  onCancel: () => void;
}

/**
 * What the current execution is doing, for somebody supervising it.
 *
 * The figures are the ones the run response already publishes - status, step
 * count, tool calls, tokens, latency, and a stable error code. Nothing is
 * derived from anything the API withheld, and nothing internal is shown: there
 * is no run id, no conversation id and no execution id on screen. Those are
 * correlation handles for a log, not information an operator acts on, and
 * printing them would invite somebody to paste one into a URL as if it were an
 * authorization.
 *
 * **Refresh is explicit.** The platform has no push or stream - the backend
 * advances a run inside the request that asked for it - so the honest options
 * are a timer or a button, and a button is the one that does not generate load
 * nobody asked for. A run waiting on a person changes when that person decides,
 * which may be hours away; polling it every few seconds would be pure noise.
 *
 * **Stop is offered only where the server accepts it.** `isCancellable` mirrors
 * the backend's own rule - `awaiting_approval` and nothing else - so the button
 * is absent rather than present-and-rejected. The server re-checks regardless;
 * this is a courtesy, not the boundary.
 */
export function ExecutionPanel({ run, busy, onRefresh, onCancel }: ExecutionPanelProps) {
  const toolCount = run.tool_calls.length;

  return (
    <section
      aria-label="Execution"
      className="rounded-xl border border-line bg-surface-muted px-4 py-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="text-xs font-semibold tracking-wide text-ink-muted uppercase">
            Execution
          </h2>
          <Badge tone={RUN_TONES[run.status]} dot={run.status === "running"}>
            {RUN_LABELS[run.status]}
          </Badge>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            className="px-2.5 py-1 text-xs"
            onClick={onRefresh}
            disabled={busy}
          >
            {busy && <Spinner />}
            Refresh
          </Button>

          {isCancellable(run.status) && (
            <Button
              variant="secondary"
              className="px-2.5 py-1 text-xs"
              onClick={onCancel}
              disabled={busy}
            >
              Stop run
            </Button>
          )}
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <Figure label="Steps" value={String(run.step_count)} />
        <Figure label="Tool calls" value={String(toolCount)} />
        <Figure label="Tokens" value={run.usage.total_tokens.toLocaleString()} />
        <Figure label="Model time" value={describeDuration(run.latency_ms)} />
      </dl>

      {run.error_code && (
        <p role="status" className="mt-3 text-sm text-danger">
          {describeErrorCode(run.error_code)}
        </p>
      )}

      {toolCount > 0 && (
        <div className="mt-3 border-t border-line pt-3">
          <ToolActivity calls={run.tool_calls} />
        </div>
      )}
    </section>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}
