"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { usePolledResource } from "@/hooks/use-polled-resource";
import { getReadiness } from "@/lib/api";
import type { DependencyCheck } from "@/types/health";

/** Turn one dependency's verdict into how it should read. */
function describe(check: DependencyCheck): { tone: "ok" | "warn" | "danger"; label: string } {
  if (check.status === "ok") return { tone: "ok", label: "ok" };
  // An optional dependency being down is expected in native development, where
  // Redis is not run at all - so it is a note, not a fault.
  return check.required
    ? { tone: "danger", label: "unavailable" }
    : { tone: "warn", label: "unavailable (optional)" };
}

/**
 * The backend's readiness report, dependency by dependency.
 *
 * A degraded verdict arrives as HTTP 503 carrying this same payload, which the
 * API client lets through rather than treating as a failure.
 */
export function ReadinessPanel() {
  const state = usePolledResource(getReadiness, { intervalMs: 15_000 });

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Dependencies</CardTitle>
          <CardDescription>Reported by the backend readiness probe.</CardDescription>
        </div>

        {state.kind === "ready" && (
          <Badge tone={state.data.status === "ready" ? "ok" : "warn"} dot>
            {state.data.status}
          </Badge>
        )}
        {state.kind === "error" && (
          <Badge tone="warn" dot>
            unknown
          </Badge>
        )}
      </CardHeader>

      {state.kind === "loading" && <p className="mt-4 text-sm text-ink-muted">Checking ...</p>}

      {state.kind === "error" && (
        <div className="mt-4 text-sm text-ink-muted">
          <p>{state.message}</p>
          {state.requestId && <p className="mt-1 font-mono text-xs">request {state.requestId}</p>}
        </div>
      )}

      {state.kind === "ready" && (
        <dl className="mt-4 divide-y divide-line border-y border-line">
          {Object.entries(state.data.dependencies).map(([name, check]) => {
            const { tone, label } = describe(check);

            return (
              <div key={name} className="flex items-center justify-between gap-4 py-2.5 text-sm">
                <dt className="font-medium">{name}</dt>
                <dd>
                  <Badge tone={tone} dot>
                    {label}
                  </Badge>
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </Card>
  );
}
