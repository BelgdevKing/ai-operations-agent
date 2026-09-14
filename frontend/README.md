# Frontend

Next.js dashboard for the platform.

Next.js (App Router) · TypeScript · Tailwind CSS v4 · shadcn/ui (later)

**Implemented so far:** a homepage confirming the frontend is running, with a
live indicator for backend reachability. No routes beyond `/`.

## Layout

```
src/
├── app/          routes (App Router)
├── components/   feature components
│   └── ui/       shadcn/ui primitives
├── lib/          API client, auth helpers, utilities
└── types/        shared TypeScript types
public/           static assets
```

## Planned screens

| Screen | Purpose |
| --- | --- |
| Conversations | Talk to an agent; watch a run stream |
| Runs | Execution history with step-by-step traces |
| Approvals | Queue of pending sensitive actions to approve or reject |
| Documents | Upload and ingestion status for the knowledge base |
| Workflows | Definitions and execution state |
| Audit | Searchable immutable activity log |
| Settings | Tenant configuration, members, roles, agent tool permissions |

## Running

Through the Compose stack, from the repository root:

```bash
docker compose up --build      # http://localhost:3000
```

Natively, with no Docker:

```powershell
cd frontend
npm install
copy .env.example .env.local   # optional - defaults point at localhost:8000
npm run dev                    # http://localhost:3000
```

Other scripts:

```powershell
npm run build                  # production build
npm run typecheck              # tsc --noEmit
```

`.env.local` is git-ignored and is where local overrides belong. Only
`NEXT_PUBLIC_*` values reach the browser, and they are inlined at build time -
restart the dev server after changing one. Full setup notes:
[docs/development-windows.md](../docs/development-windows.md).

## Rules

- Only `NEXT_PUBLIC_*` environment variables reach the browser.
- The browser talks to the backend API; it never talks to Postgres, Redis, or
  the Claude API directly.
- Server components by default; client components only where interactivity
  requires them.

See [../docs/architecture.md](../docs/architecture.md).
