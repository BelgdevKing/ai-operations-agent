import type { Metadata } from "next";

import { CurrentUserCard } from "@/components/auth/current-user-card";
import { PageHeader } from "@/components/layout/page-header";
import { ReadinessPanel } from "@/components/readiness-panel";
import { Badge } from "@/components/ui/badge";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { UsageSummary } from "@/components/usage/usage-summary";

export const metadata: Metadata = {
  title: "Dashboard",
  description: "Your session, service health and, once agents exist, their activity.",
};

/** What this page will show once the features behind it are built. */
const PLANNED = [
  {
    title: "Agent runs",
    description: "Every execution with its steps, tokens, latency and outcome.",
  },
] as const;

export default function DashboardPage() {
  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Your session and live service health. Agent activity appears here once agents exist."
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <CurrentUserCard />
        <ReadinessPanel />
        <UsageSummary />

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
