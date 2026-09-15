"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import type { ConversationSummary } from "@/types/ai";

export interface ConversationPickerProps {
  conversations: readonly ConversationSummary[];
  selectedId: string | null;
  disabled?: boolean;
  onSelect: (conversationId: string) => void;
  onStartNew: () => void;
}

/**
 * Which stored conversation is open.
 *
 * The conversations are the organization's, listed by `GET /ai/conversations`
 * and read by `GET /ai/conversations/{id}` - both tenant-scoped on the server.
 * Opening one loads it; it is never reconstructed from anything held here.
 *
 * "New conversation" clears the view rather than creating anything: a
 * conversation comes into existence when the first run is made, so an operator
 * who changes their mind has not left an empty row behind.
 *
 * A title is the user's own opening words, truncated by the backend. It is
 * shown because it is the only thing that makes a list of conversations
 * navigable, and it is rendered as text - never as markup.
 */
export function ConversationPicker({
  conversations,
  selectedId,
  disabled = false,
  onSelect,
  onStartNew,
}: ConversationPickerProps) {
  return (
    <div className="space-y-2">
      <Button
        variant={selectedId === null ? "primary" : "secondary"}
        className="w-full"
        disabled={disabled}
        onClick={onStartNew}
      >
        New conversation
      </Button>

      {conversations.length > 0 && (
        <ul aria-label="Stored conversations" className="space-y-1">
          {conversations.map((conversation) => {
            const selected = conversation.id === selectedId;

            return (
              <li key={conversation.id}>
                <button
                  type="button"
                  aria-current={selected ? "true" : undefined}
                  disabled={disabled}
                  onClick={() => onSelect(conversation.id)}
                  className={cn(
                    "w-full rounded-lg border px-3 py-2 text-left transition-colors outline-none",
                    "focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
                    "focus-visible:ring-offset-surface",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                    selected
                      ? "border-accent/50 bg-accent/5"
                      : "border-line bg-surface hover:bg-surface-muted",
                  )}
                >
                  <span className="block truncate text-sm">
                    {conversation.title ?? "Untitled conversation"}
                  </span>
                  <span className="mt-0.5 block text-xs text-ink-muted">
                    {new Date(conversation.updated_at).toLocaleString()}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
