"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { LoadingState } from "@/components/ui/states";
import { useSession } from "@/lib/auth/session-context";

/**
 * Keeps the authenticated pages out of reach of an anonymous visitor.
 *
 * **A guard, not a boundary.** It decides what to render, and rendering is not
 * a security control: the data these pages show is fetched with a bearer token
 * the backend verifies on every request. Someone who defeats this component
 * sees empty screens.
 *
 * The check is client-side because the token is held in memory - the server
 * renders this page with no way to know whether anyone is signed in, so a
 * server redirect would be guessing. Which also means a reload lands here in
 * the anonymous state and bounces to sign-in, the visible cost of not
 * persisting the token.
 *
 * No loop is possible: `/login` sits outside this guard and only redirects in
 * the other direction, and only when a session exists.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const router = useRouter();

  useEffect(() => {
    // `replace`, not `push`: a page the visitor was never allowed to see does
    // not belong in their history behind the back button.
    if (session.status === "anonymous") router.replace("/login");
  }, [session.status, router]);

  if (session.status !== "authenticated") {
    return (
      <div className="px-6 py-10">
        <LoadingState
          label={session.status === "authenticating" ? "Signing in ..." : "Redirecting to sign-in ..."}
        />
      </div>
    );
  }

  return <>{children}</>;
}
