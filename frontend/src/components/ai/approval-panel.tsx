"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorState, Spinner } from "@/components/ui/states";
import type { PendingApproval } from "@/types/ai";

export interface ApprovalPanelProps {
  approval: PendingApproval;
  /** Whether this person's role lets them decide. Not a security boundary. */
  canDecide: boolean;
  deciding: boolean;
  error: unknown;
  onApprove: () => void;
  onReject: () => void;
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
 * What is shown is the *kind* of action and why it is gated. What is not shown
 * is what the tool was asked to do: the arguments are the tenant's business
 * data and the backend does not publish them, so there is nothing here to
 * render even by mistake.
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
      </div>

      <p className="mt-2 text-sm">
        {approval.reason ??
          "The agent has asked to perform an action that needs a person to agree to it."}
      </p>

      <p className="mt-1 text-xs text-ink-muted">
        The agent is paused. Nothing has been changed, and nothing will be unless
        somebody approves it.
      </p>

      {error !== null && <ErrorState className="mt-3" error={error} />}

      {canDecide ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {confirming ? (
            <>
              <span className="text-sm font-medium">Approve this action?</span>
              <Button onClick={onApprove} disabled={deciding}>
                {deciding && <Spinner />}
                Yes, approve
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)} disabled={deciding}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              {/* Approving is the irreversible half, so it asks twice. */}
              <Button onClick={() => setConfirming(true)} disabled={deciding}>
                Approve
              </Button>
              <Button variant="secondary" onClick={onReject} disabled={deciding}>
                {deciding && <Spinner />}
                Decline
              </Button>
            </>
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm text-ink-muted">
          An administrator or owner of this organization has to decide this one.
        </p>
      )}
    </section>
  );
}
