"use client";

import { displayName } from "@/components/auth/session-menu";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuthenticatedSession } from "@/lib/auth/session-context";
import type { UserStatus } from "@/types/auth";

const STATUS_TONES: Record<UserStatus, BadgeTone> = {
  active: "ok",
  pending: "warn",
  suspended: "danger",
  deactivated: "neutral",
};

/**
 * Who is signed in, as `GET /auth/me` reported them.
 *
 * Exactly the fields that endpoint returns and no others - the backend is the
 * source of truth for the profile, and a field invented here would be a field
 * nothing keeps correct.
 */
export function CurrentUserCard() {
  const { user, memberships, active } = useAuthenticatedSession();

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Signed in</CardTitle>
          <CardDescription>From the backend, not from the browser.</CardDescription>
        </div>
        <Badge tone={STATUS_TONES[user.status]} dot>
          {user.status}
        </Badge>
      </CardHeader>

      <dl className="mt-4 divide-y divide-line border-y border-line">
        <Row label="Name" value={displayName(user)} />
        <Row label="Email" value={user.email} />
        <Row label="Organization" value={active?.organization.name ?? "None"} />
        <Row label="Role" value={active?.role ?? "none"} />
        {memberships.length > 1 && (
          <Row label="Memberships" value={`${memberships.length} organizations`} />
        )}
      </dl>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 py-2.5 text-sm">
      <dt className="font-medium">{label}</dt>
      <dd className="text-right break-all text-ink-muted">{value}</dd>
    </div>
  );
}
