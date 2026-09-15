import type { Metadata } from "next";

import { AiWorkspace } from "@/components/ai/ai-workspace";
import { PageHeader } from "@/components/layout/page-header";

export const metadata: Metadata = {
  title: "Agent console",
  description: "Supervise the platform's agents: what they were asked, what they did, and what they are waiting on.",
};

/**
 * The agent console.
 *
 * Inside the `(app)` route group, so it sits behind the same `<RequireAuth>`
 * guard and the same session as every other authenticated page. There is no
 * second authentication path here, and no organization control: which tenant a
 * request acts on comes from the session, and the backend checks it again.
 */
export default function AiPage() {
  return (
    <>
      <PageHeader
        title="Agent console"
        description="Pick an agent, open a conversation, and watch what it does. Runs are durable - a conversation left while somebody was deciding comes back exactly where it was."
      />

      <AiWorkspace />
    </>
  );
}
