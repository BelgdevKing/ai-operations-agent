"use client";

import { useId } from "react";

import { useAuthenticatedSession, useSessionStore } from "@/lib/auth/session-context";

/**
 * Choose which organization the session acts as.
 *
 * Only shown to someone who belongs to more than one, and the options are the
 * memberships `/auth/me` reported - never anything typed in. Choosing one sets
 * the `X-Organization-ID` header on subsequent requests.
 *
 * **The header is a request, not a grant.** The backend resolves it against the
 * caller's own active memberships and refuses anything else with a 403, so this
 * control cannot widen anyone's access. It exists because the backend *requires*
 * the header once a user belongs to several organizations: without it the API
 * answers 400 rather than guessing.
 */
export function OrganizationSwitcher() {
  const { memberships, active } = useAuthenticatedSession();
  const store = useSessionStore();
  const id = useId();

  if (memberships.length <= 1) return null;

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-xs font-medium text-ink-muted">
        Acting as
      </label>
      <select
        id={id}
        value={active?.organization.id ?? ""}
        onChange={(event) => store.selectOrganization(event.target.value)}
        className="w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {memberships.map((membership) => (
          <option key={membership.organization.id} value={membership.organization.id}>
            {membership.organization.name}
          </option>
        ))}
      </select>
    </div>
  );
}
