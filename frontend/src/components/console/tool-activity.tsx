"use client";

import { Badge } from "@/components/ui/badge";
import { describeTool } from "@/lib/ai/run-status";
import type { ToolCallSummary } from "@/types/ai";

export interface ToolActivityProps {
  calls: readonly ToolCallSummary[];
}

/**
 * What the agent reached for, and how each attempt ended.
 *
 * **Names and outcomes only, because that is all the API sends.**
 * `ToolCallSummary` is two fields - `tool_name` and `outcome` - and that is a
 * deliberate decision from the tool-execution phase, not an oversight: what a
 * tool was *asked* and what it *returned* are the tenant's business records,
 * they live in the conversation and the execution tables, and an activity list
 * is not the place to republish them to anyone who can see a screen.
 *
 * So there is nothing to hide here, and nothing to reconstruct. This component
 * renders two strings per row. There is no prop for arguments, no prop for a
 * payload, and no execution id: `tool_executions.id` is an internal handle the
 * run response does not publish, and an operator who needs to trace one has the
 * correlation id in the response headers and the audit trail behind it.
 *
 * Each row carries a mark, a name and a word. The mark is a convenience; the
 * word is the answer, so the state survives a monochrome display and a screen
 * reader.
 */
export function ToolActivity({ calls }: ToolActivityProps) {
  if (calls.length === 0) return null;

  return (
    <section aria-label="Tool activity" className="space-y-1.5">
      <h3 className="text-xs font-semibold tracking-wide text-ink-muted uppercase">
        Tool activity
      </h3>

      <ol className="space-y-1">
        {calls.map((call, index) => {
          const { label, tone, mark } = describeTool(call);

          return (
            <li
              key={`${call.tool_name}-${index}`}
              className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-sm"
            >
              <span aria-hidden="true" className="font-mono text-xs text-ink-muted">
                {mark}
              </span>
              <span className="font-mono text-xs">{call.tool_name}</span>
              <Badge tone={tone}>{label}</Badge>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
