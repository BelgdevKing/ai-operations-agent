import type { Metadata } from "next";

import { PageHeader } from "@/components/layout/page-header";
import { OrganizationView } from "@/components/organization/organization-view";

export const metadata: Metadata = {
  title: "Organization",
  description: "The tenant this session acts on, and its members.",
};

export default function OrganizationPage() {
  return (
    <>
      <PageHeader
        title="Organization"
        description="The tenant every request is scoped to, and who belongs to it."
      />

      <OrganizationView />
    </>
  );
}
