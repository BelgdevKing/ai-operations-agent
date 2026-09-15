import Link from "next/link";

import { BackendStatus } from "@/components/backend-status";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import { buttonClasses } from "@/components/ui/button";
import { appName } from "@/lib/config";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{appName}</h1>
        <p className="mt-3 text-ink-muted">
          Agents that understand a request, look things up, reason over business rules, call
          tools, and stop for human approval before anything sensitive happens.
        </p>
      </header>

      <div className="flex flex-wrap gap-3">
        <Link href="/dashboard" className={buttonClasses("primary")}>
          Open the dashboard
        </Link>
        <Link href="/login" className={buttonClasses("secondary")}>
          Sign in
        </Link>
        <Link href="/register" className={buttonClasses("ghost")}>
          Create an account
        </Link>
      </div>

      <BackendStatus />

      <section>
        <h2 className="text-sm font-medium tracking-wide text-ink-muted uppercase">
          Where to go
        </h2>
        <dl className="mt-3 divide-y divide-line border-y border-line">
          {NAV_ITEMS.map((item) => (
            <div key={item.href} className="flex justify-between gap-4 py-2.5 text-sm">
              <dt className="font-medium">
                <Link
                  href={item.href}
                  className="rounded text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {item.label}
                </Link>
              </dt>
              <dd className="text-right text-ink-muted">{item.description}</dd>
            </div>
          ))}
        </dl>
      </section>

      <footer className="text-sm text-ink-muted">
        Signing in is live. Agents, documents and workflows arrive in later parts.
      </footer>
    </main>
  );
}
