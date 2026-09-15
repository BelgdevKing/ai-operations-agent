import type { Metadata } from "next";

import { PageHeader } from "@/components/layout/page-header";
import { WorkflowConsole } from "@/components/workflows/workflow-console";

export const metadata: Metadata = {
  title: "Workflows",
  description: "Business processes the platform runs, and what they are waiting on.",
};

/**
 * The workflow console.
 *
 * Inside the `(app)` route group, so it sits behind the same `<RequireAuth>`
 * guard and the same session as every other authenticated page. There is no
 * second authentication path here.
 */
export default function WorkflowsPage() {
  return (
    <>
      <PageHeader
        title="Workflows"
        description="Start a process and watch what it did. A step that needs a person stops and waits, and the run continues - as the same run - once somebody decides."
      />

      <WorkflowConsole />
    </>
  );
}
