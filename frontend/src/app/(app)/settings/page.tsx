import type { Metadata } from "next";

import { CurrentUserCard } from "@/components/auth/current-user-card";
import { PageHeader } from "@/components/layout/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Notice } from "@/components/ui/notice";
import { apiUrl, appName } from "@/lib/config";

export const metadata: Metadata = {
  title: "Settings",
  description: "Your account, and how this browser session is configured.",
};

/**
 * Configuration this build was compiled with.
 *
 * Only `NEXT_PUBLIC_*` values, and only ones that are addresses or labels.
 * Anything under `NEXT_PUBLIC_` is inlined into the JavaScript the browser
 * downloads, so a secret placed there would be readable by anyone who opens
 * the page - which is why no credential belongs in frontend configuration at
 * all, shown here or not.
 */
const ENVIRONMENT = [
  { label: "API base URL", value: apiUrl, source: "NEXT_PUBLIC_API_URL" },
  { label: "Application name", value: appName, source: "NEXT_PUBLIC_APP_NAME" },
] as const;

const PLANNED = [
  { title: "Change password", description: "Not exposed by the backend yet." },
  { title: "API keys", description: "Machine credentials, shown once at creation." },
] as const;

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Your account, what this build points at, and what becomes configurable later."
      />

      <div className="space-y-4">
        <CurrentUserCard />

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Session</CardTitle>
              <CardDescription>How long you stay signed in.</CardDescription>
            </div>
            <Badge tone="warn">In memory</Badge>
          </CardHeader>

          <Notice className="mt-4">
            Your access token is held in memory for this page only. It is never written to browser
            storage or a cookie, which means nothing can read it back later - and also that
            reloading the page signs you out. Persisting it safely needs an HttpOnly cookie or a
            refresh token from the backend, neither of which exists yet.
          </Notice>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Environment</CardTitle>
              <CardDescription>Read from the build, not editable here.</CardDescription>
            </div>
          </CardHeader>

          <dl className="mt-4 divide-y divide-line border-y border-line">
            {ENVIRONMENT.map((entry) => (
              <div key={entry.source} className="flex justify-between gap-4 py-2.5 text-sm">
                <dt className="font-medium">
                  {entry.label}
                  <span className="block font-mono text-xs font-normal text-ink-muted">
                    {entry.source}
                  </span>
                </dt>
                <dd className="text-right font-mono text-xs break-all text-ink-muted">
                  {entry.value}
                </dd>
              </div>
            ))}
          </dl>

          <Notice className="mt-4">
            These are the only settings the browser receives. API keys, database passwords and
            signing secrets stay in the backend environment and are never exposed to a client.
          </Notice>
        </Card>

        <Card>
          <CardHeader>
            <div>
              <CardTitle>Appearance</CardTitle>
              <CardDescription>
                Light and dark follow the operating system preference. No switch yet.
              </CardDescription>
            </div>
            <Badge>System</Badge>
          </CardHeader>
        </Card>

        {PLANNED.map((item) => (
          <Card key={item.title}>
            <CardHeader>
              <div>
                <CardTitle>{item.title}</CardTitle>
                <CardDescription>{item.description}</CardDescription>
              </div>
              <Badge>Planned</Badge>
            </CardHeader>
          </Card>
        ))}
      </div>
    </>
  );
}
