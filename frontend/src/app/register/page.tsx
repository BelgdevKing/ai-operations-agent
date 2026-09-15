import type { Metadata } from "next";
import Link from "next/link";

import { RegisterForm } from "@/components/auth/register-form";
import { Card } from "@/components/ui/card";
import { appName } from "@/lib/config";

export const metadata: Metadata = {
  title: "Create an account",
  description: "Create an account and the organization you will own.",
};

export default function RegisterPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6 py-16">
      <header className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight">Create an account</h1>
        <p className="mt-1.5 text-sm text-ink-muted">
          You will own the organization created with it, on {appName}.
        </p>
      </header>

      <Card className="bg-surface">
        <RegisterForm />
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
