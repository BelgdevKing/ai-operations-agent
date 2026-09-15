import Link from "next/link";
import type { ReactNode } from "react";

import { OrganizationSwitcher } from "@/components/auth/organization-switcher";
import { SessionMenu } from "@/components/auth/session-menu";
import { MainNav } from "@/components/layout/main-nav";
import { appName } from "@/lib/config";

/**
 * Chrome shared by the signed-in areas of the application.
 *
 * Rendered only below `<RequireAuth>`, which is what lets the session menu
 * assume there is a session to show.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen md:grid md:grid-cols-[16rem_1fr]">
      <aside className="flex flex-col gap-5 border-b border-line bg-surface-muted px-5 py-5 md:min-h-screen md:border-r md:border-b-0">
        <Link
          href="/"
          className="block rounded text-sm font-semibold tracking-tight outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {appName}
        </Link>

        <OrganizationSwitcher />

        <MainNav />

        <div className="mt-auto border-t border-line pt-4">
          <SessionMenu />
        </div>
      </aside>

      <div className="flex min-h-screen flex-col">
        <main className="mx-auto w-full max-w-4xl flex-1 px-6 py-8">{children}</main>

        <footer className="border-t border-line px-6 py-4 text-xs text-ink-muted">
          <div className="mx-auto max-w-4xl">
            Agents, documents and workflows arrive in later parts.
          </div>
        </footer>
      </div>
    </div>
  );
}
