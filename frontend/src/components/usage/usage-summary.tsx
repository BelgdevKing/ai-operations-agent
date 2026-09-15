"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import { getUsage } from "@/lib/api";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import { isPriced, type UsageResponse } from "@/types/ai";

/**
 * What this organization has used lately.
 *
 * Deliberately one card, not a console. The agent console is the next part of
 * the roadmap and it is where charts, filters and per-day series belong; this
 * exists so the usage API has a consumer and so somebody can see the figures
 * without a terminal.
 *
 * **Nothing here is cached, stored or computed locally.** The server aggregates
 * and the server prices; this formats. In particular the cost is rendered from
 * the string the backend sent and is never parsed into a number - the whole
 * point of carrying a decimal from the price book is lost the moment a browser
 * turns it into a float.
 *
 * Keyed by the acting organization, like every other tenant-scoped view.
 */
export function UsageSummary() {
  const { active } = useAuthenticatedSession();

  if (active === null) return null;

  return <Usage key={active.organization.id} />;
}

function Usage() {
  const api = useApi();
  const usage = useAsyncResource((signal) => getUsage(api, { signal }));

  if (usage.state.kind === "loading") {
    return (
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Usage</CardTitle>
            <CardDescription>What this organization has used.</CardDescription>
          </div>
        </CardHeader>
        <LoadingState label="Loading usage ..." />
      </Card>
    );
  }

  if (usage.state.kind === "error") {
    return (
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Usage</CardTitle>
            <CardDescription>What this organization has used.</CardDescription>
          </div>
        </CardHeader>
        <ErrorState error={usage.state.error} onRetry={usage.reload} />
      </Card>
    );
  }

  return <Figures usage={usage.state.data} />;
}

function Figures({ usage }: { usage: UsageResponse }) {
  // Narrowed to the value rather than kept as a boolean: a boolean does not
  // narrow the union at the point of use, and the compiler is right to say so.
  const priced = isPriced(usage.cost) ? usage.cost : null;

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Usage</CardTitle>
          <CardDescription>
            Since {new Date(usage.since).toLocaleDateString()}.
          </CardDescription>
        </div>
        {priced ? (
          <Badge tone="ok">
            {priced.amount} {priced.currency}
          </Badge>
        ) : (
          /* Not "0.00". A deployment with no configured price knows what was
             used and not what it was worth, and saying so is the only honest
             option available to this badge. */
          <Badge>Cost not configured</Badge>
        )}
      </CardHeader>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
        <Figure label="Agent runs" value={usage.agent_runs.total} />
        <Figure label="Workflow runs" value={usage.workflow_runs.total} />
        <Figure label="Tool executions" value={usage.tool_executions.total} />
        <Figure label="Approvals" value={usage.approvals.total} />
        <Figure label="Model calls" value={usage.llm_calls} />
        <Figure label="Input tokens" value={usage.tokens.input_tokens} />
        <Figure label="Output tokens" value={usage.tokens.output_tokens} />
        <Figure label="Total tokens" value={usage.tokens.total_tokens} />
      </dl>

      {priced && priced.unpriced_calls > 0 && (
        <p className="mt-3 text-xs text-ink-muted">
          {priced.unpriced_calls} call
          {priced.unpriced_calls === 1 ? "" : "s"} could not be priced
          {priced.unpriced_models.length > 0 && ` (${priced.unpriced_models.join(", ")})`}
          , so the figure above covers only part of this usage.
        </p>
      )}
    </Card>
  );
}

function Figure({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="font-medium tabular-nums">{value.toLocaleString()}</dd>
    </div>
  );
}
