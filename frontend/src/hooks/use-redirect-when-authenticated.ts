"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useSession } from "@/lib/auth/session-context";

/**
 * Send an already-signed-in visitor away from a sign-in or registration page.
 *
 * Shared by both so they cannot drift apart - one of them silently lacking this
 * is exactly the bug it fixes.
 *
 * No loop is possible: these pages sit outside `<RequireAuth>`, so they push
 * only when a session exists, and the guard pushes only when one does not.
 * `replace` keeps a page the visitor has no further use for out of their
 * history.
 */
export function useRedirectWhenAuthenticated(destination = "/dashboard"): void {
  const session = useSession();
  const router = useRouter();

  useEffect(() => {
    if (session.status === "authenticated") router.replace(destination);
  }, [session.status, router, destination]);
}
