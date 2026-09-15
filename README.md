# AI Operations Agent Platform

A multi-tenant SaaS platform where businesses run AI agents against their own
operational data — agents that understand a request, look things up, reason over
business rules, call tools, and execute workflows, while sensitive actions stop
for human approval and everything is written to an audit log.

Open-source portfolio project. **Status: authenticated application with a
working AI generation path.** Agents, tools, RAG and workflows are not built
yet — see the roadmap below.

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

**Backend** — Python 3.12 · FastAPI · SQLAlchemy 2.x · PostgreSQL · Pydantic v2 · Alembic · Redis
**Frontend** — Next.js · TypeScript · Tailwind CSS · shadcn/ui
**AI** — Anthropic Claude API · Claude tool use · RAG · embeddings
**Infrastructure** — Docker Compose
**Testing** — pytest

Architecture is a **modular monolith**. No microservices, no Kubernetes.

---

## Repository layout

```
ai-operations-agent/
├── backend/                   # FastAPI modular monolith (Python 3.12)
│   ├── app/
│   │   ├── main.py            # application factory
│   │   ├── api/               # routers; health probes + versioned v1 API
│   │   ├── core/              # config, database, logging, middleware, errors
│   │   ├── models/            # SQLAlchemy models
│   │   ├── schemas/           # Pydantic v2 contracts
│   │   ├── repositories/      # data access
│   │   ├── services/          # business logic
│   │   └── agents,tools,workflows,knowledge,audit/   # planned areas
│   ├── alembic/               # database migrations
│   ├── tests/                 # unit + integration (pytest)
│   ├── .env.example           # native development settings
│   └── requirements*.txt      # runtime and development dependencies
├── frontend/                  # Next.js App Router dashboard (TypeScript)
│   ├── src/app/               # routes, layout, global styles
│   ├── src/components/        # UI, incl. shadcn/ui primitives later
│   ├── src/hooks/             # reusable client-side behaviour
│   ├── src/lib/               # API client, session, configuration
│   ├── src/types/             # shared TypeScript types
│   └── .env.example           # native development settings
├── infrastructure/
│   ├── docker/backend/        # backend Dockerfile (development | production)
│   ├── docker/frontend/       # frontend Dockerfile (development | production)
│   └── postgres/init/         # extensions created on first start
├── docs/                      # architecture, deployment, Windows setup, ADRs
├── .github/workflows/         # continuous integration
├── docker-compose.yml         # local development stack
├── docker-compose.prod.yml    # deployed stack
├── .env.example               # environment template for Docker
├── .env.production.example    # environment template for a deployment
└── LICENSE                    # MIT
```

Read [docs/architecture.md](docs/architecture.md) for the design in full, and
[docs/development-windows.md](docs/development-windows.md) to work without Docker.

---

## Getting started

Two supported setups. They run the same code and the same settings — only the
hostnames and the `.env` file differ.

### Option A — Docker Compose

Requires Docker and Docker Compose. Nothing else: no local Python or Node.

```bash
git clone https://github.com/BelgdevKing/ai-operations-agent.git
cd ai-operations-agent

docker compose up --build
```

| Service | URL | Notes |
| --- | --- | --- |
| Frontend | http://localhost:3000 | Next.js dev server, hot reload |
| Backend | http://localhost:8000 | FastAPI, auto-reload |
| API docs | http://localhost:8000/docs | Generated OpenAPI UI |
| Liveness | http://localhost:8000/health | `{"status":"ok", ...}` |
| Readiness | http://localhost:8000/health/ready | 503 until Postgres and Redis answer |
| PostgreSQL | `localhost:5432` | user/db `aiops`, persistent volume |
| Redis | `localhost:6379` | persistent volume |

Every setting has a working default, so the stack starts on a fresh checkout.
To override anything, `cp .env.example .env` and edit — Compose reads `.env`
automatically. Never commit `.env`; secrets come from the environment only.

Common tasks:

```bash
docker compose ps                       # status and health of each container
docker compose logs -f backend          # follow one service
docker compose exec backend pytest      # run the backend test suite
docker compose down                     # stop, keep data
docker compose down -v                  # stop and delete all data
```

### Option B — Native, no Docker

Requires Python 3.12, Node.js and a PostgreSQL install running on `localhost`.
**Redis is not needed** — nothing implemented so far uses it, and the backend
starts and reports ready without it.

Backend, in one terminal:

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Frontend, in another:

```powershell
cd frontend
npm install
npm run dev
```

One-time database setup, environment files, shell-specific activation and
troubleshooting are covered in
**[docs/development-windows.md](docs/development-windows.md)**.

### Deploying it

`docker-compose.yml` is development: it bind-mounts source, reloads on change,
and every value has a working default so a fresh checkout starts. None of that
belongs in a deployment, so a deployment has its own stack.

```bash
cp .env.production.example .env.production   # placeholders only; fill it in
docker compose --env-file .env.production -f docker-compose.prod.yml up -d
```

Runtime-only images, migrations as a step of their own, no host ports on the
database, and a deployed environment that refuses to start on any secret this
repository publishes. The procedure, the configuration, the rollback and what
to do when something is wrong are in
**[docs/deployment.md](docs/deployment.md)**.

## Roadmap

Each phase is a working slice, built in order.

- [x] **0 — Scaffolding.** Repository structure, architecture docs.
- [x] **1 — Local environment.** Docker Compose stack (FastAPI, Next.js,
      PostgreSQL, Redis), health endpoints, homepage, pytest harness, plus a
      native Windows setup without Docker.
- [x] **2 — Backend foundation.** Configuration, database and session
      management, API router structure, exception handling, structured
      logging, Alembic migrations, typed throughout.
- [x] **3 — Application schema.** 15 tables, tenant discriminator on every
      tenant-owned table, one Alembic migration.
- [x] **4 — Identity and tenancy.** Argon2id passwords, JWT access tokens,
      registration and login, organization membership with owner/admin/member
      roles, and the tenant-scoped repository pattern.
- [x] **LLM gateway.** Provider-independent abstraction over Anthropic and
      OpenAI behind a single gateway with explicit retry policy, plus an
      authenticated `POST /api/v1/ai/generate`. Built ahead of the phases below,
      which the agent work depends on.
- [x] **Frontend foundation.** App Router shell and routes, a single API
      client with the shared error envelope, and TypeScript types mirroring the
      backend contracts. Built ahead of the dashboard phase below.
- [x] **Frontend authentication.** Sign-in, registration, guarded routes, the
      current user, and organization membership administration against the
      identity API. The access token is held in memory only, so a reload signs
      you out - see [frontend/README.md](frontend/README.md#authentication).
- [x] **AI workspace.** A `/ai` screen that sends a conversation through the
      existing generation endpoint and shows the answer. Conversations are
      held in the browser only; there is no persistence yet.
- [ ] **5 — Business data.** The CRUD and search surface agents will query,
      plus row-level security behind the scoped repositories.
- [ ] **6 — Documents.** Upload, storage, chunking, embeddings, vector search.
- [ ] **7 — Agent core.** Claude tool-use loop, tool registry, run records.
- [ ] **8 — Approvals.** Sensitive-action gate, approval queue, resume-on-approve.
- [ ] **9 — Workflows.** Multi-step definitions run as durable background jobs.
- [ ] **10 — Audit & monitoring.** Immutable audit trail, run traces, cost metrics.
- [ ] **11 — Dashboard.** Next.js UI over all of the above.

## License

MIT — see [LICENSE](LICENSE).
