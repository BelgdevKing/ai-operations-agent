"use client";

import { useCallback, useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState, Spinner } from "@/components/ui/states";
import { useAsyncResource, type AsyncState } from "@/hooks/use-async-resource";
import { approveAction, listApprovals, rejectAction } from "@/lib/api";
import { describeRemaining, remainingMs } from "@/lib/approvals/expiry";
import { isAdministrator } from "@/lib/auth/permissions";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import type { ApprovalQueue, ApprovalResponse, ApprovalStatus } from "@/types/ai";

/**
 * The approval inbox.
 *
 * Everything the agent and the workflow engine have stopped to ask about, in
 * one place, oldest first - because an approval queue is a to-do list and the
 * thing that has waited longest is the thing most likely to be blocking
 * somebody.
 *
 * **What is shown is what the tool declared.** The backend publishes a one-line
 * summary and a short list of labelled values, both projected from an allow-list
 * written in the tool's own code when the approval was requested. The argument
 * payload has no representation in the response, so there is nothing here that
 * could render it.
 *
 * **What is sent is a decision and at most a reason.** The request body has one
 * field; nothing on this screen can alter what is being approved, because there
 * is no shape for it to travel in.
 *
 * Keyed by the acting organization, like every other tenant-scoped screen.
 */
export function ApprovalInbox() {
  const { active } = useAuthenticatedSession();

  if (active === null) {
    return (
      <ErrorState
        error={
          new Error("You are not acting as an active organization, so approvals are unavailable.")
        }
      />
    );
  }

  return <Inbox key={active.organization.id} canDecide={isAdministrator(active.role)} />;
}

/** Which slice of the queue is on screen. */
const VIEWS = {
  waiting: { label: "Waiting", statuses: undefined },
  decided: { label: "Decided", statuses: ["approved", "rejected"] as ApprovalStatus[] },
  closed: { label: "Closed", statuses: ["expired", "cancelled"] as ApprovalStatus[] },
} as const;

type ViewName = keyof typeof VIEWS;

function Inbox({ canDecide }: { canDecide: boolean }) {
  const api = useApi();
  const [view, setView] = useState<ViewName>("waiting");

  // Keyed by the view, so switching tab restarts the request rather than
  // leaving one slice of the queue on screen under another slice's heading.
  const queue = useAsyncResource(
    (signal) => listApprovals(api, { signal, status: VIEWS[view].statuses }),
    { key: view },
  );

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  /**
   * Decide one approval and reload the queue.
   *
   * Keyed by approval id so exactly the row being decided is disabled - and so
   * a second click on the same row while the first is in flight does nothing,
   * which matters because the backend would answer the second with a conflict
   * and the person would see an error for having been impatient.
   */
  const decide = useCallback(
    async (approval: ApprovalResponse, approve: boolean, reason: string): Promise<void> => {
      if (busy !== null) return;

      setBusy(approval.id);
      setError(null);
      try {
        if (approve) await approveAction(api, approval.id, { reason });
        else await rejectAction(api, approval.id, { reason });
        queue.reload();
      } catch (failure) {
        setError(failure);
      } finally {
        setBusy(null);
      }
    },
    [api, busy, queue],
  );

  return (
    <div className="space-y-4">
      <nav aria-label="Which approvals" className="flex flex-wrap gap-2">
        {(Object.keys(VIEWS) as ViewName[]).map((name) => (
          <Button
            key={name}
            variant={name === view ? "primary" : "ghost"}
            className="px-3 py-1 text-xs"
            onClick={() => setView(name)}
          >
            {VIEWS[name].label}
          </Button>
        ))}
      </nav>

      {error !== null && <ErrorState error={error} />}

      <Queue
        state={queue.state}
        onRetry={queue.reload}
        canDecide={canDecide && view === "waiting"}
        busy={busy}
        onDecide={decide}
      />
    </div>
  );
}

function Queue({
  state,
  onRetry,
  canDecide,
  busy,
  onDecide,
}: {
  state: AsyncState<ApprovalQueue>;
  onRetry: () => void;
  canDecide: boolean;
  busy: string | null;
  onDecide: (approval: ApprovalResponse, approve: boolean, reason: string) => void;
}) {
  if (state.kind === "loading") return <LoadingState label="Loading approvals ..." />;
  if (state.kind === "error") return <ErrorState error={state.error} onRetry={onRetry} />;

  if (state.data.approvals.length === 0) {
    return (
      <EmptyState title="Nothing here">
        When an agent or a workflow proposes something that needs a person to agree
        to it, the request waits here until somebody decides.
      </EmptyState>
    );
  }

  return (
    <ul className="space-y-3">
      {state.data.approvals.map((approval) => (
        <li key={approval.id}>
          <ApprovalCard
            approval={approval}
            canDecide={canDecide}
            busy={busy === approval.id}
            disabled={busy !== null && busy !== approval.id}
            onDecide={onDecide}
          />
        </li>
      ))}
    </ul>
  );
}

function ApprovalCard({
  approval,
  canDecide,
  busy,
  disabled,
  onDecide,
}: {
  approval: ApprovalResponse;
  canDecide: boolean;
  busy: boolean;
  disabled: boolean;
  onDecide: (approval: ApprovalResponse, approve: boolean, reason: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <article className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge tone={TONES[approval.status]} dot={approval.status === "pending"}>
          {LABELS[approval.status]}
        </Badge>
        <h2 className="text-sm font-medium">{approval.summary ?? approval.action}</h2>
        {approval.tool_name && (
          <span className="font-mono text-xs text-ink-muted">{approval.tool_name}</span>
        )}
        <Expiry approval={approval} />
      </div>

      {approval.summary_fields.length > 0 && (
        <dl className="mt-2 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
          {approval.summary_fields.map((field) => (
            <div key={field.label} className="contents">
              <dt className="text-xs text-ink-muted sm:pt-0.5">{field.label}</dt>
              <dd className="break-words">{field.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <p className="mt-2 text-sm text-ink-muted">{approval.reason}</p>

      {approval.decision_reason && (
        <p className="mt-2 text-sm">
          <span className="text-xs text-ink-muted">Decided because: </span>
          {approval.decision_reason}
        </p>
      )}

      <Button
        variant="ghost"
        className="mt-2 px-2 py-1 text-xs"
        onClick={() => setOpen((shown) => !shown)}
      >
        {open ? "Less" : "What happens either way"}
      </Button>

      {open && (
        <dl className="mt-1 space-y-1 text-sm">
          <dt className="text-xs text-ink-muted">If approved</dt>
          <dd>{approval.effect_if_approved}</dd>
          <dt className="text-xs text-ink-muted">If declined</dt>
          <dd>{approval.effect_if_rejected}</dd>
          <dt className="text-xs text-ink-muted">Waiting since</dt>
          <dd>{new Date(approval.requested_at).toLocaleString()}</dd>
        </dl>
      )}

      {canDecide && approval.status === "pending" && (
        <div className="mt-3 space-y-2 border-t border-line pt-3">
          <label className="block text-xs text-ink-muted">
            Reason (optional)
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              placeholder="Why you are deciding this way"
              disabled={busy || disabled}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus-visible:ring-2 focus-visible:ring-accent"
            />
          </label>

          <div className="flex flex-wrap items-center gap-2">
            {confirming ? (
              <>
                <span className="text-sm font-medium">
                  {approval.summary ? `${approval.summary}?` : "Approve this action?"}
                </span>
                {/* Confirms what is already on screen. It adds nothing to the
                    request - the body has one field, and it is the reason. */}
                <Button
                  onClick={() => onDecide(approval, true, reason)}
                  disabled={busy || disabled}
                >
                  {busy && <Spinner />}
                  Yes, approve
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => setConfirming(false)}
                  disabled={busy || disabled}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <>
                {/* Approving is the irreversible half, so it asks twice. */}
                <Button onClick={() => setConfirming(true)} disabled={busy || disabled}>
                  Approve
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => onDecide(approval, false, reason)}
                  disabled={busy || disabled}
                >
                  {busy && <Spinner />}
                  Decline
                </Button>
              </>
            )}
          </div>
        </div>
      )}
    </article>
  );
}

function Expiry({ approval }: { approval: ApprovalResponse }) {
  if (approval.status !== "pending") return null;

  const remaining = remainingMs(approval.expires_at);
  if (remaining === null) return null;
  if (remaining <= 0) return <Badge tone="danger">Expired</Badge>;

  return <span className="text-xs text-ink-muted">Expires in {describeRemaining(remaining)}</span>;
}

const LABELS: Record<ApprovalStatus, string> = {
  pending: "Waiting",
  approved: "Approved",
  rejected: "Declined",
  expired: "Expired",
  cancelled: "Withdrawn",
};

const TONES: Record<ApprovalStatus, BadgeTone> = {
  pending: "warn",
  approved: "ok",
  rejected: "neutral",
  expired: "danger",
  cancelled: "neutral",
};
