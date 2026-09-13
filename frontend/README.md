# Frontend

Next.js dashboard for the platform. **Not implemented yet** — this directory
currently holds the target structure only.

Next.js (App Router) · TypeScript · Tailwind CSS · shadcn/ui

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

## Rules

- Only `NEXT_PUBLIC_*` environment variables reach the browser.
- The browser talks to the backend API; it never talks to Postgres, Redis, or
  the Claude API directly.
- Server components by default; client components only where interactivity
  requires them.

See [../docs/architecture.md](../docs/architecture.md).
