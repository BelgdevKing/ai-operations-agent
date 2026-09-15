"use client";

import { MembersPanel } from "@/components/organization/members-panel";
import { OrganizationPanel } from "@/components/organization/organization-panel";
import { EmptyState } from "@/components/ui/states";
import { useAuthenticatedSession } from "@/lib/auth/session-context";

/**
 * The organization pages, or an explanation of why there are none.
 *
 * Belonging to no active organization is a state the backend can genuinely
 * report - a membership suspended, or the organization itself suspended or
 * archived - and every organization request would answer 403. Saying so is
 * better than rendering three panels that each fail separately.
 */
export function OrganizationView() {
  const { active } = useAuthenticatedSession();

  if (active === null) {
    return (
      <EmptyState title="You do not belong to an active organization">
        Your membership may have been suspended, or the organization archived. An owner or admin
        can restore access.
      </EmptyState>
    );
  }

  // Keyed by organization, so switching tenant remounts both panels and every
  // piece of their local state goes with them. Reloading the data alone is not
  // enough: a "you may not do that" error raised in one organization would
  // otherwise still be on screen in the next.
  return (
    <div className="space-y-4" key={active.organization.id}>
      <OrganizationPanel />
      <MembersPanel />
    </div>
  );
}
