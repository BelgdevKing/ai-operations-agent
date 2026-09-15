import type { Metadata } from "next";
import Link from "next/link";

import { buttonClasses } from "@/components/ui/button";

export const metadata: Metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-5 px-6 text-center">
      <p className="text-sm font-medium text-ink-muted">404</p>
      <h1 className="text-2xl font-semibold tracking-tight">This page does not exist</h1>
      <p className="text-sm text-ink-muted">
        The address may be mistyped, or the feature may not be built yet.
      </p>
      <div>
        <Link href="/" className={buttonClasses("secondary")}>
          Back to the start
        </Link>
      </div>
    </main>
  );
}
