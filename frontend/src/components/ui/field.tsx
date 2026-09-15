import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/cn";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm outline-none",
        "placeholder:text-ink-muted focus-visible:ring-2 focus-visible:ring-accent",
        "disabled:cursor-not-allowed disabled:opacity-60",
        "aria-invalid:border-danger",
        className,
      )}
      {...props}
    />
  );
}

/**
 * The id of a field's message, derived from the control's own id.
 *
 * A convention rather than a generated id, so the control can point at its
 * message with `aria-describedby` without the two having to be threaded
 * through props.
 */
export function describedById(fieldId: string): string {
  return `${fieldId}-message`;
}

export interface FieldProps {
  /** Must match the control's id, so clicking the label focuses it. */
  htmlFor: string;
  label: ReactNode;
  /** Guidance shown when there is no error. */
  hint?: ReactNode;
  /** Replaces the hint, and is what `aria-describedby` should point at. */
  error?: string;
  children: ReactNode;
}

/**
 * A labelled form control with one message slot.
 *
 * Hint and error share the slot and the id, so a control that always sets
 * `aria-describedby={describedById(id)}` is described by whichever is showing -
 * and an error is never announced as an unlabelled paragraph.
 */
export function Field({ htmlFor, label, hint, error, children }: FieldProps) {
  const messageId = describedById(htmlFor);

  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-sm font-medium">
        {label}
      </label>

      {children}

      {error ? (
        <p id={messageId} className="text-xs text-danger">
          {error}
        </p>
      ) : (
        hint && (
          <p id={messageId} className="text-xs text-ink-muted">
            {hint}
          </p>
        )
      )}
    </div>
  );
}
