"use client";

import { useCallback, useState } from "react";

import { ApprovalPanel } from "@/components/ai/approval-panel";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState, Spinner } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import {
  approveAction,
  getWorkflowRun,
  listWorkflows,
  rejectAction,
  startWorkflowRun,
} from "@/lib/api";
import { isAdministrator } from "@/lib/auth/permissions";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import type {
  WorkflowRunResponse,
  WorkflowRunStatus,
  WorkflowStepRunResponse,
  WorkflowStepStatus,
  WorkflowSummary,
} from "@/types/ai";

/**
 * The workflow console.
 *
 * The smallest thing that demonstrates the engine: pick an active workflow,
 * start it, and watch what it did - which steps ran, what each one was, and what
 * it is waiting for. When a step needs a person, the same approval panel the AI
 * workspace uses appears here, because it is the same approval.
 *
 * **Nothing a step produced is shown.** A workflow reads business records to do
 * its job, and an interface that printed every one of them so the reader could
 * watch it work would be publishing the tenant's data for the sake of a progress
 * bar. Step names, kinds and outcomes; the run's own result at the end.
 *
 * Keyed by the acting organization, like every other tenant-scoped screen.
 */
export function WorkflowConsole() {
  const { active } = useAuthenticatedSession();

  if (active === null) {
    return (
      <ErrorState
        error={new Error("You are not acting as an active organization, so workflows are unavailable.")}
      />
    );
  }

  return <Workflows key={active.organization.id} canDecide={isAdministrator(active.role)} />;
}

function Workflows({ canDecide }: { canDecide: boolean }) {
  const api = useApi();
  const workflows = useAsyncResource((signal) => listWorkflows(api, { signal }));

  if (workflows.state.kind === "loading") return <LoadingState label="Loading workflows ..." />;
  if (workflows.state.kind === "error") {
    return <ErrorState error={workflows.state.error} onRetry={workflows.reload} />;
  }

  const runnable = workflows.state.data.filter((workflow) => workflow.status === "active");

  if (runnable.length === 0) {
    return (
      <EmptyState title="No workflow is active">
        A workflow has to be drafted and activated before it can run. Activation is
        what checks that every tool and agent it names actually exists.
      </EmptyState>
    );
  }

  return (
    <div className="space-y-6">
      {runnable.map((workflow) => (
        <WorkflowCard key={workflow.id} workflow={workflow} canDecide={canDecide} />
      ))}
    </div>
  );
}

/** One workflow, and the last run of it this page started. */
function WorkflowCard({
  workflow,
  canDecide,
}: {
  workflow: WorkflowSummary;
  canDecide: boolean;
}) {
  const api = useApi();

  const [run, setRun] = useState<WorkflowRunResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [reference, setReference] = useState("");

  /**
   * Run whatever was asked and record the answer.
   *
   * Every call goes through here so "busy" and "error" have one definition, and
   * so a failure never leaves the button spinning.
   */
  const perform = useCallback(
    async (call: () => Promise<WorkflowRunResponse | null>): Promise<void> => {
      setBusy(true);
      setError(null);
      try {
        const next = await call();
        if (next !== null) setRun(next);
      } catch (failure) {
        setError(failure);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const start = useCallback(() => {
    const input = reference.trim() === "" ? {} : { shipment_reference: reference.trim() };
    void perform(() => startWorkflowRun(api, workflow.id, input));
  }, [api, perform, reference, workflow.id]);

  const refresh = useCallback(() => {
    if (run === null) return;
    void perform(() => getWorkflowRun(api, workflow.id, run.run_id));
  }, [api, perform, run, workflow.id]);

  const decide = useCallback(
    (approve: boolean, reason: string) => {
      const approval = run?.approval;
      if (!approval) return;

      void perform(async () => {
        const decision = approve
          ? await approveAction(api, approval.id, { reason })
          : await rejectAction(api, approval.id, { reason });
        // The decision names what it resumed. A workflow approval always
        // resumes a workflow; anything else here would be a backend change
        // nobody told this screen about.
        return decision.workflow_run;
      });
    },
    [api, perform, run],
  );

  return (
    <section className="rounded-xl border border-line bg-surface px-4 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">{workflow.name}</h2>
          {workflow.description && (
            <p className="mt-0.5 text-sm text-ink-muted">{workflow.description}</p>
          )}
        </div>
        <Badge>v{workflow.version}</Badge>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="flex-1 text-xs text-ink-muted">
          Shipment reference
          <input
            value={reference}
            onChange={(event) => setReference(event.target.value)}
            placeholder="ABC123"
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus-visible:ring-2 focus-visible:ring-accent"
          />
        </label>
        <Button onClick={start} disabled={busy}>
          {busy && <Spinner />}
          Start run
        </Button>
        {run !== null && (
          <Button variant="secondary" onClick={refresh} disabled={busy}>
            Refresh
          </Button>
        )}
      </div>

      {error !== null && <ErrorState className="mt-3" error={error} />}

      {run !== null && <RunView run={run} canDecide={canDecide} busy={busy} onDecide={decide} />}
    </section>
  );
}

function RunView({
  run,
  canDecide,
  busy,
  onDecide,
}: {
  run: WorkflowRunResponse;
  canDecide: boolean;
  busy: boolean;
  onDecide: (approve: boolean, reason: string) => void;
}) {
  return (
    <div className="mt-4 space-y-3">
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
        <Badge tone={RUN_TONES[run.status]} dot>
          {RUN_LABELS[run.status]}
        </Badge>
        <span>
          {run.step_count} {run.step_count === 1 ? "step" : "steps"}
        </span>
        {run.current_step && <span>at {run.current_step}</span>}
        {run.error_code && <span className="text-danger">{run.error_code}</span>}
      </p>

      {run.steps.length > 0 && (
        <ol className="space-y-1.5">
          {run.steps.map((step) => (
            <StepRow key={step.id} step={step} />
          ))}
        </ol>
      )}

      {run.approval !== null && (
        <ApprovalPanel
          approval={{
            id: run.approval.id,
            tool_name: run.approval.tool_name,
            action: run.approval.action,
            reason: run.approval.reason,
            summary: run.approval.summary,
            summary_fields: run.approval.summary_fields,
            expires_at: run.approval.expires_at,
            requested_at: run.approval.requested_at,
            requested_by: run.approval.requested_by,
          }}
          canDecide={canDecide}
          deciding={busy}
          error={null}
          onApprove={(reason) => onDecide(true, reason)}
          onReject={(reason) => onDecide(false, reason)}
        />
      )}
    </div>
  );
}

function StepRow({ step }: { step: WorkflowStepRunResponse }) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-line px-3 py-2 text-sm">
      <span className="text-xs text-ink-muted">{step.position}</span>
      <span className="font-medium">{step.step_key}</span>
      <span className="font-mono text-xs text-ink-muted">{STEP_KINDS[step.step_type]}</span>
      <Badge tone={STEP_TONES[step.status]}>{step.status.replace("_", " ")}</Badge>
      {step.error_code && <span className="text-xs text-danger">{step.error_code}</span>}
    </li>
  );
}

/** Words for a status, rather than the enum's own spelling. */
const RUN_LABELS: Record<WorkflowRunStatus, string> = {
  pending: "Not started",
  running: "Running",
  awaiting_approval: "Waiting for approval",
  succeeded: "Finished",
  failed: "Stopped",
  cancelled: "Cancelled",
};

const RUN_TONES: Record<WorkflowRunStatus, BadgeTone> = {
  pending: "neutral",
  running: "accent",
  awaiting_approval: "warn",
  succeeded: "ok",
  failed: "danger",
  cancelled: "neutral",
};

const STEP_TONES: Record<WorkflowStepStatus, BadgeTone> = {
  pending: "neutral",
  running: "accent",
  awaiting_approval: "warn",
  succeeded: "ok",
  failed: "danger",
  skipped: "neutral",
  cancelled: "neutral",
};

/** What each kind of step is, in a word a reader does not have to decode. */
const STEP_KINDS: Record<WorkflowStepRunResponse["step_type"], string> = {
  tool_call: "tool",
  agent_step: "agent",
  condition: "branch",
  approval: "approval",
};
