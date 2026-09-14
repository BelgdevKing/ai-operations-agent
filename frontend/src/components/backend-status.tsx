"use client";

import { useEffect, useState } from "react";

import { apiUrl } from "@/lib/config";
import type { HealthResponse } from "@/types/health";

type State =
  | { kind: "checking" }
  | { kind: "up"; health: HealthResponse }
  | { kind: "down"; reason: string };

/**
 * Development aid: polls the backend liveness endpoint so the homepage shows
 * whether the whole Compose stack is wired together. No business logic.
 */
export function BackendStatus() {
  const [state, setState] = useState<State>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;

    const check = async () => {
      try {
        const response = await fetch(`${apiUrl}/health`, { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const health = (await response.json()) as HealthResponse;
        if (!cancelled) setState({ kind: "up", health });
      } catch (error) {
        if (!cancelled) {
          setState({
            kind: "down",
            reason: error instanceof Error ? error.message : "unreachable",
          });
        }
      }
    };

    void check();
    const timer = setInterval(check, 10_000);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const dot =
    state.kind === "up"
      ? "bg-ok"
      : state.kind === "down"
        ? "bg-warn"
        : "bg-ink-muted";

  return (
    <div className="rounded-lg border border-line bg-surface-muted p-4">
      <div className="flex items-center gap-2">
        <span className={`inline-block h-2 w-2 rounded-full ${dot}`} aria-hidden />
        <span className="text-sm font-medium">Backend API</span>
      </div>

      <p className="mt-2 text-sm text-ink-muted">
        {state.kind === "checking" && `Checking ${apiUrl} ...`}
        {state.kind === "up" &&
          `Reachable - ${state.health.service} v${state.health.version} (${state.health.environment})`}
        {state.kind === "down" &&
          `Not reachable at ${apiUrl} (${state.reason}). Start it with: docker compose up`}
      </p>
    </div>
  );
}
