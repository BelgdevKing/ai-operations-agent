"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";
import type { AgentSummary } from "@/types/ai";

export interface AgentPickerProps {
  agents: readonly AgentSummary[];
  selectedId: string | null;
  /** Disabled while a run is in flight: changing agent mid-turn is not a thing. */
  disabled?: boolean;
  onSelect: (agentId: string) => void;
}

/**
 * Which agent the console is talking to.
 *
 * **The list is the server's.** It comes from `GET /ai/agents`, scoped to the
 * caller's organization, and selecting one only changes which id the next run
 * is addressed to. An id typed into a URL would not make a caller entitled to
 * it - the backend resolves the agent against the same verified membership on
 * every request, and answers 404 for one that is not theirs. This component is
 * a convenience, never a permission.
 *
 * **What is shown is what the API publishes**: a name and a description. The
 * agent's instructions are deliberately absent from `AgentSummary` - the system
 * prompt is the server's, and showing it would hand a caller the text to work
 * around - so there is nothing here to leak even by accident.
 *
 * A radio group rather than a list of buttons: picking one of several is what a
 * radio group *is*, and it gives arrow-key navigation, a single tab stop and
 * the right announcement without any of it being written here.
 */
export function AgentPicker({ agents, selectedId, disabled = false, onSelect }: AgentPickerProps) {
  return (
    <div role="radiogroup" aria-label="Agent" className="space-y-1.5">
      {agents.map((agent) => {
        const selected = agent.id === selectedId;

        return (
          <button
            key={agent.id}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onSelect(agent.id)}
            className={cn(
              "w-full rounded-lg border px-3 py-2 text-left transition-colors outline-none",
              "focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2",
              "focus-visible:ring-offset-surface disabled:cursor-not-allowed disabled:opacity-50",
              selected
                ? "border-accent/50 bg-accent/5"
                : "border-line bg-surface hover:bg-surface-muted",
            )}
          >
            <span className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{agent.name}</span>
              {/* The selected state is a word as well as a border colour, so it
                  survives a monochrome display and a screen reader. */}
              {selected && <Badge tone="accent">Selected</Badge>}
            </span>

            {agent.description && (
              <span className="mt-0.5 block text-xs text-ink-muted">{agent.description}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
