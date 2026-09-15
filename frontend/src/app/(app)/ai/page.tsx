import type { Metadata } from "next";

import { AiWorkspace } from "@/components/ai/ai-workspace";
import { PageHeader } from "@/components/layout/page-header";

export const metadata: Metadata = {
  title: "AI workspace",
  description: "Ask the platform's configured model a question.",
};

/**
 * The AI workspace.
 *
 * Inside the `(app)` route group, so it sits behind the same `<RequireAuth>`
 * guard and the same session as every other authenticated page. There is no
 * second authentication path here.
 */
export default function AiPage() {
  return (
    <>
      <PageHeader
        title="AI workspace"
        description="Ask a question and the platform answers through its own AI gateway. The conversation lives in this tab only."
      />

      <AiWorkspace />
    </>
  );
}
