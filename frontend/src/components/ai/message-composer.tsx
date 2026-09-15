"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/states";
import { MAX_MESSAGE_CHARACTERS, type Capacity } from "@/lib/ai/conversation";

/** Start warning about length before the limit, not at it. */
const COUNTER_THRESHOLD = MAX_MESSAGE_CHARACTERS * 0.8;

export interface MessageComposerProps {
  generating: boolean;
  /** Judged against the conversation this composer belongs to. */
  capacityOf: (draft: string) => Capacity;
  onSend: (content: string) => void;
  onCancel: () => void;
}

/**
 * The message box.
 *
 * It owns the draft. That is the whole reason it is a separate component: with
 * the text held here, typing re-renders one textarea instead of the entire
 * transcript above it.
 */
export function MessageComposer({
  generating,
  capacityOf,
  onSend,
  onCancel,
}: MessageComposerProps) {
  const [draft, setDraft] = useState("");

  const capacity = capacityOf(draft);
  const length = draft.trim().length;

  function submit(): void {
    if (!capacity.canSend) return;
    onSend(draft);
    setDraft("");
  }

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    submit();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    // Enter sends, Shift+Enter makes a new line. `isComposing` keeps an input
    // method editor's Enter - which is how CJK text is committed - from being
    // read as a submission.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <form method="post" onSubmit={onSubmit} className="mt-4 space-y-2">
      <label htmlFor="ai-message" className="sr-only">
        Message
      </label>

      <textarea
        id="ai-message"
        name="message"
        rows={3}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={onKeyDown}
        disabled={capacity.full}
        placeholder={capacity.full ? "This conversation is full." : "Ask something ..."}
        aria-describedby="ai-composer-hint"
        aria-invalid={capacity.reason ? true : undefined}
        className="w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 text-sm outline-none placeholder:text-ink-muted focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-60"
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p id="ai-composer-hint" className="text-xs text-ink-muted">
          {capacity.reason ?? "Enter to send, Shift+Enter for a new line."}
        </p>

        <div className="flex items-center gap-2">
          {length > COUNTER_THRESHOLD && (
            <span
              className={
                length > MAX_MESSAGE_CHARACTERS ? "text-xs text-danger" : "text-xs text-ink-muted"
              }
            >
              {length.toLocaleString()} / {MAX_MESSAGE_CHARACTERS.toLocaleString()}
            </span>
          )}

          {generating ? (
            <Button variant="secondary" onClick={onCancel}>
              <Spinner />
              Stop
            </Button>
          ) : (
            <Button type="submit" disabled={!capacity.canSend}>
              Send
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
