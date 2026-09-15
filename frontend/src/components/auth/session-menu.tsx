"use client";

import { useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAuthenticatedSession, useSessionStore } from "@/lib/auth/session-context";
import type { UserProfile } from "@/types/auth";

/** A user's display name, falling back to the address when they gave none. */
export function displayName(user: UserProfile): string {
  const full = [user.first_name, user.last_name].filter(Boolean).join(" ").trim();
  return full || user.email;
}

/**
 * Who is signed in, which organization they are acting as, and the way out.
 *
 * Signing out drops the only copy of the token and sends the visitor to the
 * sign-in page. There is no backend call to make: the token is stateless, so
 * there is nothing on the server to invalidate, and inventing an endpoint to
 * pretend otherwise would be worse than saying so.
 */
export function SessionMenu() {
  const { user, active } = useAuthenticatedSession();
  const store = useSessionStore();
  const router = useRouter();

  function signOut(): void {
    store.signOut();
    router.replace("/login");
  }

  return (
    <div className="space-y-3">
      <div>
        <p className="truncate text-sm font-medium" title={displayName(user)}>
          {displayName(user)}
        </p>
        <p className="truncate text-xs text-ink-muted" title={user.email}>
          {user.email}
        </p>
      </div>

      {active ? (
        <Badge tone="accent">{active.role}</Badge>
      ) : (
        <Badge tone="warn" dot>
          No organization
        </Badge>
      )}

      <Button variant="secondary" className="w-full" onClick={signOut}>
        Sign out
      </Button>
    </div>
  );
}
