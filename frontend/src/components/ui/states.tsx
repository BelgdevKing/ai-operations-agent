/**
 * The four states every data-dependent screen owes the reader: loading,
 * success, empty, and error.
 *
 * Success is the screen's own job. The other three live here so they look the
 * same everywhere, and so that "empty" is never quietly rendered as a blank
 * area the user reads as broken.
 */

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { describeError } from "@/lib/api/presentation";
import { cn } from "@/lib/cn";

/**
 * A spinning indicator.
 *
 * Decorative by default. Wherever it accompanies visible text - a button that
 * says "Signing in ...", a row that says "Thinking ..." - naming it too would
 * have a screen reader read the same thing twice. Pass `label` only where the
 * spinner is the *only* sign that something is happening.
 */
export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <>
      <span
        aria-hidden
        className={cn(
          "inline-block h-4 w-4 animate-spin rounded-full border-2 border-line border-t-accent",
          className,
        )}
      />
      {label && <span className="sr-only">{label}</span>}
    </>
  );
}

export function LoadingState({ label = "Loading ..." }: { label?: string }) {
  return (
    // role="status" announces the wait; the words carry it, not the animation.
    <div role="status" className="flex items-center gap-2.5 py-8 text-sm text-ink-muted">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line px-4 py-8 text-center">
      <p className="text-sm font-medium">{title}</p>
      {children && <div className="mt-1 text-sm text-ink-muted">{children}</div>}
    </div>
  );
}

/**
 * A failure, described by the shared mapping.
 *
 * The component never decides what to say - `describeError` does, once, for
 * every screen - so a 403 reads the same here as anywhere else, and a 5xx says
 * nothing about what happened on the server.
 */
export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const { title, message, tone, requestId } = describeError(error);

  return (
    <div
      role="alert"
      className={cn(
        "rounded-lg border px-4 py-3 text-sm",
        tone === "danger"
          ? "border-danger/30 bg-danger/10"
          : "border-warn/30 bg-warn/10",
        className,
      )}
    >
      <p className="font-medium text-ink">{title}</p>
      <p className="mt-1 text-ink-muted">{message}</p>

      {requestId && (
        <p className="mt-1 font-mono text-xs text-ink-muted">request {requestId}</p>
      )}

      {onRetry && (
        <Button variant="secondary" className="mt-3" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
