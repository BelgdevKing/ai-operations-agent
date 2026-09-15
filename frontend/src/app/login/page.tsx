import type { Metadata } from "next";
import Link from "next/link";

import { LoginForm } from "@/components/auth/login-form";
import { Card } from "@/components/ui/card";
import { appName } from "@/lib/config";

export const metadata: Metadata = {
  title: "Sign in",
  description: "Sign in to the platform.",
};

export default function LoginPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-6 px-6 py-16">
      <header className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="mt-1.5 text-sm text-ink-muted">to {appName}</p>
      </header>

      <Card className="bg-surface">
        <LoginForm />
      </Card>

      <p className="text-center text-sm text-ink-muted">
        <Link
          href="/"
          className="rounded outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"
        >
          Back to the start
        </Link>
      </p>
    </main>
  );
}
