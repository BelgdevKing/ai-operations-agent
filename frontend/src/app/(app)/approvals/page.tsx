import type { Metadata } from "next";

import { ApprovalInbox } from "@/components/approvals/approval-inbox";
import { PageHeader } from "@/components/layout/page-header";

export const metadata: Metadata = {
  title: "Approvals",
  description: "Actions the platform has stopped to ask a person about.",
};

/**
 * The approval inbox.
 *
 * Inside the `(app)` route group, so it sits behind the same `<RequireAuth>`
 * guard and the same session as every other authenticated page. There is no
 * second authentication path here, and no second authorization one either -
 * whether somebody may decide is settled by the backend on every request.
 */
export default function ApprovalsPage() {
  return (
    <>
      <PageHeader
        title="Approvals"
        description="Everything an agent or a workflow has stopped to ask about. Nothing here has happened yet - a request waits until somebody decides, or until it expires."
      />

      <ApprovalInbox />
    </>
  );
}
