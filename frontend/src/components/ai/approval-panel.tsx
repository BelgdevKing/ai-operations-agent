"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorState, Spinner } from "@/components/ui/states";
import { describeRemaining, remainingMs } from "@/lib/approvals/expiry";
import type { PendingApproval } from "@/types/ai";

export interface ApprovalPanelProps {
  approval: PendingApproval;
  /** Whether this person's role lets them decide. Not a security boundary. */
  canDecide: boolean;
  deciding: boolean;
  error: unknown;
  onApprove: (reason: string) => void;
  onReject: (reason: string) => void;
}

/**
 * What the agent wants to do, and the two answers to it.
 *
 * **The buttons are not the boundary.** Whether a person may approve is decided
 * by the backend, against their role in the database, on every request. This
 * component hides the controls from somebody who cannot use them so they are
 * not shown something that would fail - a courtesy, and nothing more. A member
 * who posted to the approve path directly would still get a 403.
 *
 * What is shown is the action, the record it lands on, and why it is gated.
 * What is not shown is the argument payload: the backend publishes a summary
 * the *tool* declared - which of its own fields a reviewer may see - and there
 * is no field on the response carrying anything else, so there is nothing here
 * to render by mistake.
 *
 * Every value below goes through JSX, which escapes it. Nothing is injected as
 * markup, and nothing is fetched or linked from the text - a summary is a
 * model's arguments reduced to plain text, and plain text is where it stays.
 */
export function ApprovalPanel({
  approval,
  canDecide,
  deciding,
  error,
  onApprove,
  onReject,
}: ApprovalPanelProps) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <section
      aria-label="Action awaiting approval"
      className="mt-4 rounded-xl border border-warn/40 bg-warn/5 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="warn" dot>
          Waiting for approval
        </Badge>
        {approval.tool_name && (
          <span className="font-mono text-xs text-ink-muted">{approval.tool_name}</span>
        )}
        <Expiry at={approval.expires_at} />
      </div>

      {approval.summary && <p className="mt-2 text-sm font-medium">{approval.summary}</p>}

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

      <p className="mt-2 text-sm text-ink-muted">
        {approval.reason ??
          "The agent has asked to perform an action that needs a person to agree to it."}
      </p>

      <p className="mt-1 text-xs text-ink-muted">
        Nothing has been changed, and nothing will be unless somebody approves it.
      </p>

      {error !== null && <ErrorState className="mt-3" error={error} />}

      {canDecide ? (
        <div className="mt-3 space-y-2">
          <label className="block text-xs text-ink-muted">
            Reason (optional)
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              placeholder="Why you are deciding this way"
              disabled={deciding}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus-visible:ring-2 focus-visible:ring-accent"
            />
          </label>

          <div className="flex flex-wrap items-center gap-2">
            {confirming ? (
              <>
                <span className="text-sm font-medium">
                  {approval.summary ? `${approval.summary}?` : "Approve this action?"}
                </span>
                {/* Confirms the approval already on screen. It sends no new
                    information about the action, and cannot: the decision
                    request has one field, and it is the reason. */}
                <Button onClick={() => onApprove(reason)} disabled={deciding}>
                  {deciding && <Spinner />}
                  Yes, approve
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => setConfirming(false)}
                  disabled={deciding}
                >
                  Cancel
                </Button>
              </>
            ) : (
              <>
                {/* Approving is the irreversible half, so it asks twice. */}
                <Button onClick={() => setConfirming(true)} disabled={deciding}>
                  Approve
                </Button>
                <Button variant="secondary" onClick={() => onReject(reason)} disabled={deciding}>
                  {deciding && <Spinner />}
                  Decline
                </Button>
              </>
            )}
          </div>
        </div>
      ) : (
        <p className="mt-3 text-sm text-ink-muted">
          An administrator or owner of this organization has to decide this one.
        </p>
      )}
    </section>
  );
}

/**
 * How long is left, or that there is none.
 *
 * Rendered from the timestamp on each paint rather than ticked down by a timer:
 * a countdown that keeps running is a countdown that keeps re-rendering, and the
 * deadline is hours away. The backend decides what is expired - this only says
 * what the row says.
 */
function Expiry({ at }: { at: string | null }) {
  const remaining = remainingMs(at);

  if (remaining === null) return null;
  if (remaining <= 0) return <Badge tone="danger">Expired</Badge>;

  return <span className="text-xs text-ink-muted">Expires in {describeRemaining(remaining)}</span>;
}
