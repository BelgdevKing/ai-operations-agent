"use client";

import { Badge } from "@/components/ui/badge";
import { usePolledResource } from "@/hooks/use-polled-resource";
import { getHealth } from "@/lib/api";
import { apiUrl } from "@/lib/config";

/**
 * Development aid: polls the backend liveness endpoint so a page can show
 * whether the two halves of the stack are talking. No business logic.
 */
export function BackendStatus() {
  const state = usePolledResource(getHealth, { intervalMs: 10_000 });

  return (
    <div className="rounded-lg border border-line bg-surface-muted p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium">Backend API</span>

        {state.kind === "loading" && <Badge dot>Checking</Badge>}
        {state.kind === "ready" && (
          <Badge tone="ok" dot>
            Reachable
          </Badge>
        )}
        {state.kind === "error" && (
          <Badge tone="warn" dot>
            Unreachable
          </Badge>
        )}
      </div>

      <p className="mt-2 text-sm text-ink-muted">
        {state.kind === "loading" && `Checking ${apiUrl} ...`}
        {state.kind === "ready" &&
          `${state.data.service} v${state.data.version} (${state.data.environment})`}
        {state.kind === "error" &&
          `${state.message} Start it with: uvicorn app.main:app --reload`}
      </p>
    </div>
  );
}
