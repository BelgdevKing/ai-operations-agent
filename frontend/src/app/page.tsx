import { BackendStatus } from "@/components/backend-status";
import { appName } from "@/lib/config";

const stack = [
  { name: "Frontend", detail: "Next.js + TypeScript + Tailwind CSS" },
  { name: "Backend", detail: "FastAPI on Python 3.12" },
  { name: "Database", detail: "PostgreSQL with a persistent volume" },
  { name: "Cache / queue", detail: "Redis" },
];

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
          {appName}
        </h1>
        <p className="mt-3 flex items-center gap-2 text-ink-muted">
          <span className="inline-block h-2 w-2 rounded-full bg-ok" aria-hidden />
          Frontend is running.
        </p>
      </header>

      <BackendStatus />

      <section>
        <h2 className="text-sm font-medium uppercase tracking-wide text-ink-muted">
          Local stack
        </h2>
        <dl className="mt-3 divide-y divide-line border-y border-line">
          {stack.map((item) => (
            <div key={item.name} className="flex justify-between gap-4 py-2.5 text-sm">
              <dt className="font-medium">{item.name}</dt>
              <dd className="text-right text-ink-muted">{item.detail}</dd>
            </div>
          ))}
        </dl>
      </section>

      <footer className="text-sm text-ink-muted">
        No authentication, agents, or AI features yet - this step sets up the
        development environment only.
      </footer>
    </main>
  );
}
