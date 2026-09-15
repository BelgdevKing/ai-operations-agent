"use client";

import { Badge } from "@/components/ui/badge";
import { EmptyState, Spinner } from "@/components/ui/states";
import type { AssistantTurn, Turn } from "@/lib/ai/conversation";
import { cn } from "@/lib/cn";

export interface ConversationViewProps {
  turns: readonly Turn[];
  generating: boolean;
}

/**
 * The transcript.
 *
 * Message text is rendered as **plain text**, with `whitespace-pre-wrap` to
 * keep the model's line breaks. No Markdown, no HTML: rendering a model's
 * output as markup is how an injected instruction becomes an injected element,
 * and nothing here needs formatting badly enough to take that on.
 */
export function ConversationView({ turns, generating }: ConversationViewProps) {
  if (turns.length === 0 && !generating) {
    return (
      <EmptyState title="Nothing asked yet">
        Describe what you need in your own words. The conversation lasts as long as this page does.
      </EmptyState>
    );
  }

  return (
    <div
      // A log, so a screen reader announces each answer as it arrives rather
      // than re-reading the whole conversation.
      role="log"
      aria-live="polite"
      aria-busy={generating}
      aria-label="Conversation"
      className="space-y-4"
    >
      {turns.map((turn) => (
        <Message key={turn.id} turn={turn} />
      ))}

      {generating && <Thinking />}
    </div>
  );
}

function Message({ turn }: { turn: Turn }) {
  const isUser = turn.role === "user";

  return (
    <article
      // The role is stated in text as well as shown by placement and border,
      // so it survives both a screen reader and a monochrome display.
      aria-label={isUser ? "Your message" : "Assistant message"}
      className={cn(
        "rounded-xl border px-4 py-3",
        isUser ? "border-line bg-surface-muted sm:ml-8" : "border-accent/30 bg-surface sm:mr-8",
      )}
    >
      <p className="mb-1.5 text-xs font-semibold tracking-wide text-ink-muted uppercase">
        {isUser ? "You" : "Assistant"}
      </p>

      <p className="text-sm break-words whitespace-pre-wrap">{turn.content}</p>

      {!isUser && <AnswerFooter turn={turn} />}
    </article>
  );
}

/** What produced the answer, from the fields the endpoint actually returns. */
function AnswerFooter({ turn }: { turn: AssistantTurn }) {
  return (
    <p className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
      <Badge>{turn.model}</Badge>
      <span>{turn.usage.total_tokens.toLocaleString()} tokens</span>
      <span>{(turn.latencyMs / 1000).toFixed(1)}s</span>
    </p>
  );
}

function Thinking() {
  return (
    <article
      aria-label="Assistant is responding"
      className="rounded-xl border border-accent/30 bg-surface px-4 py-3 sm:mr-8"
    >
      <p className="mb-1.5 text-xs font-semibold tracking-wide text-ink-muted uppercase">
        Assistant
      </p>
      <p className="flex items-center gap-2 text-sm text-ink-muted">
        <Spinner />
        Thinking ...
      </p>
    </article>
  );
}
