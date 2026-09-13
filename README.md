# AI Operations Agent Platform

A multi-tenant SaaS platform where businesses run AI agents against their own
operational data — agents that understand a request, look things up, reason over
business rules, call tools, and execute workflows, while sensitive actions stop
for human approval and everything is written to an audit log.

Open-source portfolio project. **Status: scaffolding — no application code yet.**

---

## What it is meant to do

| Capability | Description |
| --- | --- |
| Understand business requests | Natural-language input turned into a structured intent |
| Search business data | Query tenant records through safe, scoped tools |
| Search company documents | Retrieval over uploaded documents (RAG) |
| Use tools and APIs | Claude tool use over an explicit, permissioned tool registry |
| Reason over business rules | Tenant-configurable policies constrain what an agent may do |
| Execute workflows | Multi-step operations run as durable, resumable jobs |
| Request human approval | Sensitive actions pause and wait for an approver |
| Maintain audit logs | Every decision, tool call, and approval is recorded immutably |
| Monitor agent execution | Per-run traces: steps, tokens, latency, cost, outcome |

---

## Technology

**Backend** — Python 3.14 · FastAPI · SQLAlchemy 2.x · PostgreSQL · Pydantic v2 · Alembic · Redis
**Frontend** — Next.js · TypeScript · Tailwind CSS · shadcn/ui
**AI** — Anthropic Claude API · Claude tool use · RAG · embeddings
**Infrastructure** — Docker Compose
**Testing** — pytest

Architecture is a **modular monolith**. No microservices, no Kubernetes.

---

## Repository layout

```
ai-operations-agent/
├── backend/               # FastAPI modular monolith
│   ├── app/
│   │   ├── api/v1/        # HTTP routers, versioned
│   │   ├── core/          # config, database, security, logging
│   │   ├── modules/       # business modules (see backend/README.md)
│   │   ├── shared/        # cross-cutting helpers
│   │   └── workers/       # background job handlers
│   ├── alembic/           # database migrations
│   └── tests/             # unit + integration (pytest)
├── frontend/              # Next.js App Router dashboard
│   ├── src/app/           # routes
│   ├── src/components/    # UI, incl. shadcn/ui primitives
│   ├── src/lib/           # API client, utilities
│   └── src/types/         # shared TypeScript types
├── infrastructure/        # Dockerfiles, Postgres init scripts
├── docs/                  # architecture notes and ADRs
├── docker-compose.yml     # local stack (placeholder)
├── .env.example           # environment variable template
└── LICENSE                # MIT
```

Read [docs/architecture.md](docs/architecture.md) for the design in full.

---

## Getting started

Requires Docker and Docker Compose.

```bash
git clone https://github.com/BelgdevKing/ai-operations-agent.git
cd ai-operations-agent

cp .env.example .env       # then edit .env
docker compose up -d postgres redis
```

Postgres listens on `5432`, Redis on `6379`. The `backend` and `frontend`
services are commented out in `docker-compose.yml` until those applications
exist.

Never commit `.env`. Secrets come from the environment only.

---

## Roadmap

Each phase is a working slice, built in order.

- [x] **0 — Scaffolding.** Repository structure, docs, Docker placeholder.
- [ ] **1 — Foundation.** FastAPI app, config, database session, health check, migrations, test harness.
- [ ] **2 — Multi-tenancy.** Tenant model, tenant-scoped repositories, request-scoped tenant context.
- [ ] **3 — Identity.** Users, JWT auth, roles, tenant membership.
- [ ] **4 — Business data.** Domain entities and the CRUD + search surface agents will query.
- [ ] **5 — Documents.** Upload, storage, chunking, embeddings, vector search (RAG).
- [ ] **6 — Agent core.** Claude tool-use loop, tool registry, run records, streaming.
- [ ] **7 — Approvals.** Sensitive-action gate, approval queue, resume-on-approve.
- [ ] **8 — Workflows.** Multi-step definitions executed as durable background jobs.
- [ ] **9 — Audit & monitoring.** Immutable audit trail, run traces, token/cost metrics.
- [ ] **10 — Dashboard.** Next.js UI over all of the above.

---

## License

MIT — see [LICENSE](LICENSE).
