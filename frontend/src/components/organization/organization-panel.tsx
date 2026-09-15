"use client";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import { getOrganization } from "@/lib/api/endpoints";
import { useApi, useAuthenticatedSession } from "@/lib/auth/session-context";
import type { OrganizationStatus } from "@/types/organization";

const STATUS_TONES: Record<OrganizationStatus, BadgeTone> = {
  active: "ok",
  suspended: "warn",
  archived: "neutral",
};

/**
 * The organization this session acts on.
 *
 * Fetched rather than read from the session's membership summary, because the
 * endpoint is the source of truth and carries fields the summary does not.
 * Which organization it returns is decided by the backend from the caller's
 * verified membership - there is no id in the request for anyone to change.
 */
export function OrganizationPanel() {
  const { active } = useAuthenticatedSession();
  const api = useApi();

  const { state, reload } = useAsyncResource((signal) => getOrganization(api, { signal }), {
    key: active?.organization.id ?? "",
  });

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Organization</CardTitle>
          <CardDescription>The tenant every request on these pages is scoped to.</CardDescription>
        </div>
        {state.kind === "ready" && (
          <Badge tone={STATUS_TONES[state.data.status]} dot>
            {state.data.status}
          </Badge>
        )}
      </CardHeader>

      {state.kind === "loading" && <LoadingState label="Loading organization ..." />}

      {state.kind === "error" && (
        <ErrorState className="mt-4" error={state.error} onRetry={reload} />
      )}

      {state.kind === "ready" && (
        <dl className="mt-4 divide-y divide-line border-y border-line">
          <Row label="Name" value={state.data.name} />
          <Row label="Slug" value={state.data.slug} mono />
          <Row label="Your role" value={active?.role ?? "none"} />
          <Row label="Created" value={new Date(state.data.created_at).toLocaleString()} />
        </dl>
      )}
    </Card>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-4 py-2.5 text-sm">
      <dt className="font-medium">{label}</dt>
      <dd className={mono ? "text-right font-mono text-xs text-ink-muted" : "text-right text-ink-muted"}>
        {value}
      </dd>
    </div>
  );
}
