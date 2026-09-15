"use client";

import { useEffect } from "react";

import { Button } from "@/components/ui/button";

/**
 * The route error boundary.
 *
 * It shows the digest rather than the error itself. Next.js already replaces
 * server error messages with an opaque digest in a production build, and
 * printing whatever is left invites a stack trace or a connection string onto
 * the screen. The digest is enough to find the matching server log.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // The browser console is the developer's copy; nothing is sent anywhere.
    console.error(error);
  }, [error]);

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-5 px-6 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">Something went wrong</h1>
      <p className="text-sm text-ink-muted">
        This page could not be rendered. Trying again is usually worth it; if it keeps happening,
        the server log has the detail.
      </p>

      {error.digest && (
        <p className="font-mono text-xs text-ink-muted">error {error.digest}</p>
      )}

      <div>
        <Button onClick={reset}>Try again</Button>
      </div>
    </main>
  );
}
