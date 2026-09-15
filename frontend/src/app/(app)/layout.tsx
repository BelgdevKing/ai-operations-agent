import type { ReactNode } from "react";

import { RequireAuth } from "@/components/auth/require-auth";
import { AppShell } from "@/components/layout/app-shell";

/**
 * Chrome for the application routes.
 *
 * A route group, so `/dashboard`, `/organization` and `/settings` share this
 * layout without `(app)` appearing in any URL. The landing, sign-in and
 * registration pages stay outside it and keep their own bare layout.
 *
 * The guard wraps the shell rather than only the page, because the shell shows
 * the signed-in user: rendering it for an anonymous visitor would be rendering
 * something that does not exist yet.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
