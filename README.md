# AI Operations Agent Platform

**Self-hosted AI agents that act on your business data — and stop for a human
before they do anything destructive.**

A multi-tenant backend and console where an AI agent answers an operational
question, looks the answer up through permissioned tools, and — when it wants to
change something — pauses and waits for a person to approve it. Every run, tool
call, approval and token is recorded.

Open-source, MIT, self-hosted. Built as a portfolio project and developed in
public.

### The distinction that matters

> **The model proposes. The platform disposes.**
>
> A language model emits a tool name and arguments — nothing more. The platform
> resolves the name in a registry, rejects reserved argument names, validates
> the arguments against a schema, checks the tool is permitted, stops for a
> human if the tool is destructive, executes it itself under a timeout, bounds
> the result, and records what happened.
>
> There is no path from model output to execution that skips those steps. The
> model never holds authority, a credential or a database connection.

The whole boundary is one readable file:
[`backend/app/tools/executor.py`](backend/app/tools/executor.py).

**Evaluating it?** [docs/evaluation.md](docs/evaluation.md) answers what is
implemented, where the trust boundary is, and what to test first.
**Wondering if it fits your problem?** [docs/use-cases.md](docs/use-cases.md).

---

## The problem

Operations teams answer the same questions all day — *where is this shipment,
what does this customer owe, why was this invoice held* — by clicking through
internal systems. A chatbot bolted onto those systems is easy to build and hard
to trust: it can be talked into doing something, it keeps no record of what it
did, and nobody can tell afterwards whether a customer's order was cancelled by
a person or by a language model.

This project takes the other approach. The model chooses **which** tool to call;
the platform decides **whether** that call is allowed, executes it itself, and
stops the destructive ones until a named human approves.

## Who it is for

- **Engineers evaluating agent architecture** — a complete, readable
  implementation of tool use, durable runs, approvals and tenancy, with 2,074
  tests written against the boundaries rather than the plumbing.
- **Teams who want agents over internal data without sending it to a SaaS** —
  everything runs on your own host, against your own PostgreSQL.
- **Anyone who has to explain to a compliance officer what the agent did** —
  runs, steps, tool executions, approvals and token cost are all durable rows.

It is **not** a finished commercial product, and nobody is running it in
production. See [Project status](#project-status).

## Why this architecture

Nine properties, each chosen because the obvious alternative fails in a way
that is expensive to discover later. All of them are implemented.

- **Tools execute server-side.** The model names one; the platform runs it.
  Handing execution to the model means an injected instruction is an executed
  instruction.
- **Destructive actions stop for a person.** A tool declared `destructive`
  cannot opt out — the metadata refuses to validate if it tries, so the mistake
  is caught when the tool is written rather than when it fires.
- **Authorization is tenant-aware and server-side.** The active organization
  arrives in a header that is checked against membership every request. It
  selects a tenant; it never grants one.
- **Execution state is durable.** A run is rows, not memory. One that pauses
  for an approval survives the request that started it, and a person can decide
  hours later.
- **Concurrency is settled in the database.** Decisions and executions are
  claimed by conditional `UPDATE`; `rowcount == 1` is the proof of winning. Two
  approvers clicking at once produce one decision, not two cancellations.
- **Cost is accounted per tenant.** Derived from the execution tables at read
  time rather than accumulated into a counter that can drift from what
  happened. An unpriced model reports *unknown*, never zero.
- **Telemetry carries no identity.** No tenant id, no execution id, no prompt,
  no tool argument, no result — in a log, a metric or a span. Enforced by
  allow-lists, and asserted by tests against real runs.
- **Failure is bounded and legible.** Every external wait has a ceiling. A
  dependency outage answers `503`, not `500`, because a client and an on-call
  engineer act differently on the two.
- **Self-hosted, with no vendor lock.** Your host, your PostgreSQL, your choice
  of Anthropic or OpenAI behind one gateway — and which one served a request is
  deliberately absent from every durable row.

The reasoning behind each is in [docs/architecture.md](docs/architecture.md);
how to check them is in [docs/evaluation.md](docs/evaluation.md).

---

## What it does today

Everything in this table is implemented and covered by tests.

| Capability | What is actually there |
| --- | --- |
| **Agent runs** | A tool-use loop with a bounded step budget. Each run is a durable row: status, steps, tool calls, tokens, latency, stable error code. |
| **Tool execution** | A registry of typed tools with declared safety classes. The platform validates arguments against a schema, enforces a per-call timeout and bounds the result size. The model never executes anything itself. |
| **Human approval** | A tool marked `destructive` pauses the run and writes an approval row. An admin approves or rejects; the same run then continues. The gated action runs at most once, enforced by a conditional UPDATE rather than by application logic. |
| **Workflows** | Multi-step definitions — tool calls, agent steps, conditions, approval gates — executed as a durable state machine that survives the request that started it. |
| **Conversations** | Server-side conversation and message history, so a run can continue where the last one stopped. |
| **Multi-tenancy** | Shared schema with an `organization_id` discriminator. One repository base class is the only place the tenant filter is written, and composite foreign keys make a cross-tenant reference unrepresentable in the database. |
| **Identity** | Argon2id passwords, JWT access tokens, organizations with owner/admin/member roles. The active tenant comes from a header that is always checked against the caller's membership. |
| **Usage and cost** | Runs, steps, tool executions and approvals aggregated per organization, with token cost from a configurable price book. Money is `Decimal`, serialised as a string. Unpriced models report *unknown* rather than zero. |
| **Metrics** | A dependency-free Prometheus endpoint, off by default, behind a bearer token. Bounded label sets with an overflow bucket — no tenant id and no execution id ever becomes a label. |
| **Tracing** | Spans at six boundaries with W3C trace-context propagation, off by default. A 23-key attribute allow-list, and any UUID-shaped value is dropped whatever key it arrives under. |
| **Reliability** | Every external wait is bounded and configurable. A database outage answers `503 service_unavailable`, not `500`. The model gateway is the only thing that retries, and it retries only transport failures. |
| **Console** | A Next.js app: sign-in, agent console with live execution detail, approval inbox, workflows, usage, organization members. The access token is held in memory only. |
| **Deployment** | Multi-stage production images, a separate deployed Compose stack, migrations as a one-shot job, and a runbook. |

### Not implemented

Stated plainly, because the repository contains placeholders for some of it:

- **Document retrieval (RAG).** There is a `documents` table holding metadata
  and an empty `app/knowledge/` package. No upload, extraction, chunking,
  embedding or vector search exists.
- **An audit API or UI.** Audit events are written to the database; nothing
  reads them back over HTTP yet.
- **PostgreSQL row-level security.** Tenant isolation is enforced in the
  repository layer and by database constraints, not by RLS policies.
- **Token revocation, user invitations, password reset, billing, rate
  limiting.**

---

## How it fits together

```
  Browser ──► Next.js console ──► FastAPI
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
        Agent runtime         Workflow engine       Approval service
              │                     │                     │
              └─────────┬───────────┴──────────┬──────────┘
                        ▼                      ▼
                  Tool executor           LLM gateway
                        │                      │
                        ▼                      ▼
                   PostgreSQL          Anthropic / OpenAI
```

A **modular monolith**: one deployable, layered by responsibility
(`api → services → repositories → models`), with the agent runtime, tool
framework, workflow engine and observability as peer packages. No
microservices, no queue, no Kubernetes.

The rules that hold it together:

- **The model is untrusted input.** Its output selects a tool by name and
  supplies arguments; both are validated before anything runs.
- **Nothing is trusted from the client.** Tenant, identity and role are
  resolved server-side on every request.
- **No transaction is held across an LLM call, a tool call, or a human wait.**
- **Metadata and content are stored separately.** Operational tables hold ids,
  counts, codes and timings; conversation content lives apart from them.

Full design: [docs/architecture.md](docs/architecture.md).

## Security and tenant isolation

| | |
| --- | --- |
| Tenant filter | One place — the scoped repository base class |
| Cross-tenant references | Composite foreign keys `(id, organization_id)`; unrepresentable, not merely checked |
| Active organization | `X-Organization-ID`, verified against membership on every request; it selects, it never grants |
| Passwords | Argon2id above the OWASP minimum |
| Tokens | JWT, HMAC only — algorithm confusion is refused by configuration |
| Browser storage | None. No token in `localStorage`, `sessionStorage`, cookies or IndexedDB |
| Secrets | Environment only. Never in an image, a log, a health response, a metric or a span |
| Telemetry | No tenant id, no execution id, no prompt, no tool argument, no result |
| Deployed start-up | Refuses the shipped JWT secret, the shipped database password, `DEBUG`, and wildcard CORS |

Verified by tests, including cross-tenant access attempts and assertions that
telemetry carries no identifier.

## Technology

| | |
| --- | --- |
| Backend | Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · PostgreSQL 17 · Pydantic v2 · Alembic |
| Frontend | Next.js 15 · React 19 · TypeScript · Tailwind CSS v4 |
| AI | Anthropic and OpenAI behind one provider-independent gateway |
| Infrastructure | Docker Compose |
| Testing | pytest · Node's built-in test runner |

Redis is in the stack for later use; today it backs the readiness check only.

---

## Quick start

### Docker Compose

Requires Docker and Docker Compose. Nothing else.

```bash
git clone https://github.com/BelgdevKing/ai-operations-agent.git
cd ai-operations-agent
docker compose up --build
```

Every setting has a working default, so this starts on a fresh checkout. To
override anything, `cp .env.example .env` and edit it.

| | |
| --- | --- |
| Console | http://localhost:3000 |
| API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness | http://localhost:8000/health/ready |

```bash
docker compose exec backend alembic upgrade head   # create the schema
docker compose exec backend pytest                 # run the suite
docker compose logs -f backend
docker compose down                                # add -v to delete data
```

### Native

Requires Python 3.12, Node.js 18.18 or newer (CI and the container image use
22) and a PostgreSQL you can connect to. Backend:

```bash
cd backend
py -3.12 -m venv .venv          # python3 -m venv .venv on macOS/Linux
.venv\Scripts\activate          # source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then set DATABASE_URL
alembic upgrade head            # required - nothing creates the schema for you
uvicorn app.main:app --reload
```

Frontend, in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Creating the database, shell-specific activation and troubleshooting:
[docs/development-windows.md](docs/development-windows.md).

### Configuration

Every setting has a default and is documented in
[backend/.env.example](backend/.env.example), and in
[.env.example](.env.example) for the Docker stack. The ones that matter first:

| Setting | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | local PostgreSQL | Must start `postgresql+asyncpg://` |
| `ANTHROPIC_API_KEY` | unset | Needed only to *execute* an agent |
| `LLM_PROVIDER` | `anthropic` | Or `openai` |
| `JWT_SECRET_KEY` | development default | A deployed environment refuses to start on it |
| `METRICS_ENABLED` | `false` | Requires `METRICS_TOKEN` when true |
| `TRACING_ENABLED` | `false` | Spans to the log, using OpenTelemetry field names |

The application starts without a model provider key. Everything except running
an agent works; a run then fails with a clear configuration error.

---

## See it working in five minutes

The repository ships a demo dataset — two organizations with customers,
shipments, charges and invoices — so there is something for an agent to answer
questions about.

**1. Create an account** at http://localhost:3000/register, or over the API:

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-password",
       "organization_name":"Acme Operations"}'
```

Registration returns the user and the organization it created. Sign in
separately for a token.

**2. Load the demo data** and attach your account to it:

```bash
cd backend
python -m scripts.seed_demo_data --attach-user you@example.com
```

Idempotent — every row's id derives from its business key — and it refuses to
run unless `APP_ENV=development`.

**3. Open the console** at http://localhost:3000/ai, pick **Operations
assistant**, and ask:

> Check shipment ABC123 and tell me if there are outstanding charges.

The agent calls `get_shipment` and `get_shipment_charges`, then answers. The
execution panel shows the step count, tool calls, tokens and model time, and
each tool call with its outcome.

**4. Ask for something destructive:**

> Cancel shipment ABC123.

`cancel_shipment` is declared `destructive`, so the run **stops**. Its status
becomes `awaiting_approval` and an approval appears at
http://localhost:3000/approvals showing the action, the subject and an
allow-listed summary of the arguments — never the raw tool payload.

**5. Approve or reject it.** Approving resumes the same run and the shipment is
cancelled exactly once; rejecting resumes it too, and the agent reports that
nothing was cancelled. Either way the decision, who made it and when are
durable.

**6. See what it cost** at http://localhost:3000/dashboard, or:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/ai/usage
```

Runs, tool executions, approvals and tokens for your organization. Cost appears
once `LLM_PRICING` is configured; until then it reports *unknown* rather than
guessing.

**7. Optional — watch the machinery.** With `TRACING_ENABLED=true`, one request
produces a span tree: the HTTP request, the agent run, each model call, each
tool execution. With `METRICS_ENABLED=true` and a `METRICS_TOKEN`, `/metrics`
serves Prometheus text.

Steps 1, 2, 6 and 7 need no model provider credential. Steps 3 to 5 do.

---

## Development

```bash
# Backend
cd backend
ruff check app tests scripts
ruff format --check app tests scripts
mypy                              # strict; every function typed
alembic check                     # models and schema agree
pytest                            # 1,781 tests

# Frontend
cd frontend
npm run lint
npm run typecheck
npm test                          # 293 tests, Node's built-in runner
npm run build
```

Integration tests need a reachable PostgreSQL and skip themselves without one.
CI runs all of the above plus both production image builds — see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

How to contribute: [CONTRIBUTING.md](CONTRIBUTING.md).
Reporting a vulnerability: [SECURITY.md](SECURITY.md).

## Deployment

`docker-compose.yml` is development — bind mounts, hot reload, working
defaults. A deployment has its own stack:

```bash
cp .env.production.example .env.production   # placeholders only; fill it in
docker compose --env-file .env.production -f docker-compose.prod.yml up -d
```

Runtime-only images, migrations as their own job, no host ports on the
database, and a deployed environment that refuses to start on any secret this
repository publishes. Procedure, rollback, backups and troubleshooting:
[docs/deployment.md](docs/deployment.md).

---

## Project status

**Implemented and tested; not hardened by real traffic.** The platform runs end
to end locally and in a Compose deployment. It has never been run against
production load, has had no security audit, and has no users.

| | |
| --- | --- |
| Tests | 1,781 backend · 293 frontend |
| API routes | 30 |
| Database tables | 23, across 7 migrations |
| Built-in tools | 5 — four read-only, one destructive |
| Built-in agents | 1 |

Verified locally: the full test suite, type checking, linting, the migration
chain, and the authenticated API surface exercised end to end without a model
credential. **Not verified:** browser testing (no browser in the development
environment), live model-provider calls (no credential configured), container
image builds (no Docker daemon in the development environment — CI covers
them), load testing, penetration testing.

## Roadmap

Built in order, each a working slice:

- [x] Scaffolding, local environment, backend foundation, application schema
- [x] Identity and multi-tenancy
- [x] LLM gateway — Anthropic and OpenAI behind one interface
- [x] Frontend foundation, authentication and workspace
- [x] Business data and the scoped repository pattern
- [x] Agent core — tool-use loop, tool registry, durable run records
- [x] Conversation persistence
- [x] Workflow engine — durable multi-step execution
- [x] Human-in-the-loop approvals
- [x] Observability, cost and usage accounting
- [x] Frontend Agent Console
- [x] Production deployment, CI, and reliability hardening

Not started:

- [ ] Documents and retrieval — upload, chunking, embeddings, vector search
- [ ] An audit API and UI over the audit trail that is already recorded
- [ ] Rate limiting and per-tenant quotas
- [ ] User invitations and password reset

---

## If this is useful to your team

The platform is MIT licensed and self-hostable in full — no key, no account, no
hosted dependency. Everything described above works without paying anyone.

There is **no paid tier, no hosted service and no support contract today.** The
list below is what commercial work around a project like this could look like,
not a set of products being sold. If any of it ever becomes real, it will be
stated here plainly.

| Potential service | What it would involve |
| --- | --- |
| **Deployment and integration** | Standing the platform up in your environment and connecting it to the systems it needs to read and act on. |
| **Custom tools** | Tools against your systems, with the safety classes and approval policy that match your risk. |
| **Workflow implementation** | Operational procedures built on the existing execution and approval architecture. |
| **Architecture review** | Tenant isolation, approval semantics, agent safety, observability and reliability — for teams building something comparable. |
| **Custom development** | Extending the platform for a specific operational environment. |

> **Commercial contact route: not configured yet.**
>
> No email address, contact form or scheduling link is published in this
> repository, and none has been invented for it. GitHub Issues is the only
> route that exists today. Establishing one is an
> [open decision](#decisions-not-yet-made) for the project owner.

No customers, revenue, partnerships, production deployments or prior engagements
exist. Nothing here should be read as implying otherwise.

## Decisions not yet made

Recorded openly rather than answered with a placeholder:

- A contact address or form for commercial enquiries and security reports.
- Whether to offer paid deployment, integration or development work at all.
- Whether to publish a hosted version, and on what terms.
- Whether the project moves to a GitHub organization, and keeps this name.
- The repository description, topics and social preview image — all GitHub
  settings, none of which this repository can configure for itself.
- Whether to enable Discussions, private vulnerability reporting, and GitHub
  Sponsors or another funding route.
- Whether to adopt a code of conduct — deferred until there are contributors
  for one to govern.

None of these has been decided or assumed anywhere in the repository.

## License

MIT — see [LICENSE](LICENSE).
