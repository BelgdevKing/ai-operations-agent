# Architecture

**Status:** partly built. The backend foundation exists - configuration,
database and session management, the API router structure, exception handling,
structured logging, health endpoints and Alembic migrations. Sections marked
*planned* are the design the rest of the code will be built toward.

---

## 1. Goals and non-goals

### Goals

1. **Multi-tenant from day one.** Tenant isolation is a property of the data
   access layer, not something bolted on later.
2. **Agents that act, not just chat.** An agent can read business data, search
   documents, call tools, and start workflows.
3. **Safe by default.** Any action that changes state or touches sensitive data
   passes through a policy check and, where required, a human approver.
4. **Fully auditable.** Every agent run, tool call, approval, and state change is
   recorded and replayable.
5. **Maintainable by one developer.** A modular monolith that a newcomer can run
   with `docker compose up`.

### Non-goals

- Microservices. Modules are separated by code boundaries, not network calls.
- Kubernetes or any orchestration beyond Docker Compose.
- Training or hosting models. The platform calls the Claude API.
- Real-time collaborative editing, billing, or a plugin marketplace.

---

## 2. Architectural style

A **modular monolith**: one deployable FastAPI application internally divided
into modules with explicit boundaries.

```mermaid
flowchart TB
    subgraph Client
        FE["Next.js Dashboard<br/>TypeScript · Tailwind · shadcn/ui"]
    end

    subgraph Backend["FastAPI Modular Monolith"]
        API["API layer · /api/v1"]
        MW["Middleware<br/>auth · tenant context · request id"]
        MOD["Services · repositories · models"]
        WRK["Background workers"]
    end

    subgraph Data
        PG[("PostgreSQL<br/>relational + pgvector")]
        RD[("Redis<br/>cache · queue · rate limit")]
    end

    subgraph External
        CL["Anthropic Claude API"]
        EMB["Embedding API"]
        EXT["Tenant systems / APIs"]
    end

    FE -->|HTTPS JSON| API
    API --> MW
    MW --> MOD
    MOD --> PG
    MOD --> RD
    MOD --> CL
    MOD --> EMB
    MOD --> EXT
    RD --> WRK
    WRK --> MOD
```

**Why a monolith.** The hard problems here are tenant isolation, agent
correctness, and auditability — none of which get easier by adding a network
between components. One process means one transaction boundary, one migration
history, and one place to debug. Module boundaries are kept clean so that if a
piece ever needs to be extracted, the seam already exists.

### Layering inside the backend

```
Request
   |
   v
API layer         routers, request/response schemas, HTTP status mapping
   |
   v
Service layer     business logic, orchestration, policy checks, transactions
   |
   v
Repository layer  data access - every query tenant-scoped
   |
   v
Model layer       SQLAlchemy 2.x ORM models
```

Rules:

- Routers contain no business logic; services contain no SQL.
- One area reaches another **through its service only** — never by importing
  its models or querying its tables.
- Pydantic v2 models are the contract at the API edge; SQLAlchemy models never
  leave the service layer.

Section 3 sets out how this maps onto packages.

---

## 3. Code structure

The backend is organised by **layer**, with the domain areas that are more than
a layer given their own package.

```
backend/app/
├── main.py         application factory, middleware, router mounting
├── core/           configuration, database, cache, logging, middleware, errors
├── api/            HTTP layer
│   ├── deps.py     shared dependencies (settings, session, services)
│   ├── health.py   probe endpoints, deliberately unversioned
│   └── v1/         versioned API; endpoints/ holds one module per resource
├── models/         SQLAlchemy models: Base, mixins, one module per aggregate
├── schemas/        Pydantic v2 request/response contracts
├── repositories/   data access; every query goes through one of these
├── services/       business logic, orchestration, policy checks
├── agents/         Claude tool-use loop, run records          (not built yet)
├── tools/          the tool registry an agent may call        (not built yet)
├── workflows/      workflow definitions and state machine     (not built yet)
├── knowledge/      document ingestion and retrieval (RAG)     (not built yet)
└── audit/          append-only audit trail and run traces     (not built yet)
```

A request moves down the layers and back:

```
api/        routers: validate, call a service, shape the response. No logic.
services/   the business operation. Raises domain errors, never HTTPException.
repositories/ queries. Tenant-scoped, so isolation is enforced in one place.
models/     the tables.
```

Rules that keep the layers from blurring:

- Routers contain no business logic; services contain no SQL.
- Services never import FastAPI. They raise `core.exceptions` errors, which the
  API layer translates — that is what makes a service reusable from a worker or
  an agent tool, not just from HTTP.
- Pydantic schemas are the contract at the edge; SQLAlchemy models never leave
  the service layer.
- A service may call another service. Nothing calls another area's repository.

### Why layer-first rather than a package per domain

Grouping by layer puts the cross-cutting rules where they can be enforced: one
`BaseRepository` is the single place tenant scoping is applied, and one error
hierarchy is the single place failures are defined. The domain areas that carry
real machinery rather than plain CRUD — agents, tools, workflows, knowledge,
audit — get their own package, because their internals are not a layer and do
not belong spread across four of them.

### Dependency direction

```
agents ──┬──> knowledge     (retrieval)
         ├──> tools         (execution)
         ├──> workflows     (start / resume)
         └──> audit         (record everything)

workflows ──> audit
services ──> repositories ──> models
everything ──> core
```

`core` and `audit` are leaves: they depend on nothing above them. Cycles are not
allowed — if two areas need each other, the shared concept belongs in `core/` or
in a third area.

---

## 4. Multi-tenancy

**Strategy: shared database, shared schema, discriminator column.** Every
tenant-owned table carries a non-null `tenant_id` foreign key.

This is chosen over schema-per-tenant or database-per-tenant because a single
migration history and connection pool is far simpler to operate, and the
isolation guarantee can be enforced in one place in code.

### Enforcement — defence in depth

1. **Tenant resolution (middleware).** Each request resolves exactly one tenant,
   from a JWT claim (default), an `X-Tenant-ID` header, or a subdomain. The
   result is stored in a request-scoped context. No tenant, no access.
2. **Scoped repositories (primary guard).** Repositories take the tenant id from
   the request context and add `WHERE tenant_id = :tenant_id` to every query. A
   base repository class provides this; modules do not write raw unscoped
   queries.
3. **Postgres row-level security (backstop).** RLS policies on tenant tables,
   driven by a session variable set per connection checkout — so a bug in
   application code cannot leak one tenant's rows to another.
4. **Tests as a guarantee.** A shared test suite asserts, for every tenant-owned
   table, that tenant A cannot read or write tenant B's rows.

### Keys and indexes

Primary keys are UUIDs. Tenant-owned tables index `(tenant_id, <lookup column>)`
rather than the lookup column alone, so every query is index-covered under the
tenant filter.

---

## 5. Core data model (sketch)

```mermaid
erDiagram
    TENANT ||--o{ USER : "has members"
    TENANT ||--o{ AGENT : defines
    TENANT ||--o{ DOCUMENT : owns
    TENANT ||--o{ WORKFLOW : defines
    TENANT ||--o{ AUDIT_LOG : records

    USER ||--o{ CONVERSATION : starts
    AGENT ||--o{ AGENT_RUN : executes
    CONVERSATION ||--o{ MESSAGE : contains
    CONVERSATION ||--o{ AGENT_RUN : triggers

    AGENT_RUN ||--o{ TOOL_CALL : makes
    AGENT_RUN ||--o{ APPROVAL_REQUEST : raises
    AGENT_RUN ||--o{ AUDIT_LOG : writes

    WORKFLOW ||--o{ WORKFLOW_EXECUTION : "instantiated as"
    WORKFLOW_EXECUTION ||--o{ WORKFLOW_STEP_RUN : contains
    WORKFLOW_STEP_RUN ||--o{ APPROVAL_REQUEST : "may require"

    DOCUMENT ||--o{ DOCUMENT_CHUNK : "split into"
    APPROVAL_REQUEST ||--o| APPROVAL_DECISION : "resolved by"
```

Conventions on every table: `id` (UUID), `tenant_id` where tenant-owned,
`created_at`, `updated_at`. Soft deletes via `deleted_at` where history matters.
Audit rows are append-only — no update, no delete.

---

## 6. Request lifecycle

```
1. Request arrives      ->  request id assigned, structured log opened
2. Auth middleware      ->  JWT verified, user identity loaded
3. Tenant middleware    ->  tenant resolved, membership checked,
                            request-scoped context populated
4. Router               ->  payload validated by a Pydantic model
5. Dependencies         ->  DB session opened, services constructed
6. Service              ->  permission check, business logic, repository calls
7. Repository           ->  tenant-scoped queries
8. Response             ->  ORM objects mapped to response schemas
9. Teardown             ->  transaction committed or rolled back,
                            audit entries flushed, log closed
```

Domain exceptions are translated to HTTP responses in one exception-handler
layer, so services never import `HTTPException`.

---

## 7. Agent execution (planned)

An agent run is a bounded loop around the Claude API using **Claude tool use**.

```mermaid
sequenceDiagram
    participant U as User
    participant API as API
    participant E as Agent engine
    participant C as Claude API
    participant T as Tool registry
    participant AP as Approvals
    participant AU as Audit

    U->>API: business request
    API->>E: start run (tenant, user, agent)
    E->>AU: run started
    loop until final answer, max iterations, or timeout
        E->>C: messages + tool definitions
        C-->>E: text and/or tool_use blocks
        alt tool requested
            E->>T: resolve tool, check policy
            alt sensitive action
                T->>AP: create approval request
                AP-->>E: run paused and persisted
            else allowed
                T-->>E: tool result
            end
            E->>AU: record tool call and result
        else final answer
            E->>AU: run completed
        end
    end
    E-->>U: answer plus trace
```

Design points:

- **Tool registry.** Tools are declared with a JSON schema, a handler, a
  sensitivity level, and the roles allowed to invoke them. Claude only ever sees
  the tools the current tenant and user are permitted to use.
- **Every run is a database record.** State (`running`, `awaiting_approval`,
  `completed`, `failed`, `cancelled`), message history, and tool calls are
  persisted, so a paused run can resume in a different process.
- **Bounded.** Iteration cap, wall-clock timeout, and token budget per run, all
  configurable per tenant.
- **Business rules.** Tenant-configured rules are enforced in code before a tool
  executes — never left to the model to respect on its own.

---

## 8. Retrieval (RAG, planned)

**Ingestion:** upload → store file → extract text → chunk (size and overlap from
config) → embed each chunk → persist chunk and vector with `tenant_id`. This runs
as a background job; documents carry an ingestion status.

**Query:** embed the query → vector similarity search in Postgres (`pgvector`)
filtered by `tenant_id` → optional keyword/trigram search for a hybrid result →
merge and rank → hand the top chunks to the agent as tool output, each with a
citation back to its source document.

`pgvector` rather than a dedicated vector database: one datastore, one backup,
one transaction boundary, and tenant filtering that uses the same mechanism as
every other query. Revisit only if recall or latency demands it.

---

## 9. Approvals and workflows (planned)

**Approvals.** A tool or workflow step marked sensitive creates an approval
request instead of executing. The request records who asked, what action, with
what arguments, and why it was gated. An approver with the right role approves or
rejects; approval resumes the paused run with the original arguments — the
arguments are never re-derived by the model after the fact.

**Workflows.** A workflow is an ordered set of steps (tool call, agent step,
approval gate, or condition) stored as a definition and executed as a durable
state machine. Each step run is persisted, so an execution survives a restart and
can be retried or resumed from its last completed step. Long-running executions
are driven by Redis-backed background workers rather than the request thread.

---

## 10. Audit and monitoring (planned)

**Audit log.** Append-only. Each entry holds tenant, actor (user or agent), the
action, the target entity, before/after state where applicable, a correlation id
linking it to its agent run, and a timestamp. Nothing updates or deletes audit
rows — retention is handled by partitioning, not mutation.

**Run traces.** Per agent run: every iteration, prompt, tool call, latency, token
usage, estimated cost, and final outcome — enough to answer "why did the agent do
that?" after the fact.

**Operational telemetry.** Structured JSON logs carrying request id, tenant id
and user id on every line; health endpoints (`/health` for liveness,
`/health/ready` checking Postgres and Redis); counters and histograms for request
rate, error rate, agent run duration, tool failures, and token spend per tenant.

---

## 11. Security

- **Secrets from the environment only.** No credential is committed; `.env` is
  ignored and `.env.example` documents every variable.
- **Authentication.** JWT access tokens plus refresh tokens; passwords hashed
  with a modern KDF.
- **Authorization.** Role-based within a tenant (owner, admin, operator, viewer),
  checked in the service layer. Tenant membership is verified on every request,
  not trusted from the token alone.
- **Tenant isolation.** Section 4 — scoped repositories with RLS as a backstop.
- **Input validation.** Pydantic v2 at the boundary; SQLAlchemy parameter binding
  throughout — no string-built SQL.
- **Agent containment.** Tools are an allowlist. The model cannot reach the
  database, the filesystem, or the network except through a registered tool, and
  every tool call is policy-checked and logged.
- **Prompt injection.** Retrieved document content and tool output are treated as
  untrusted data. Permission to act comes from the registry and the policy layer,
  never from instructions found inside content.
- **Transport and limits.** TLS at the edge, CORS restricted to configured
  origins, per-tenant rate limiting in Redis.

---

## 12. Testing

`pytest` throughout, against a real PostgreSQL instance (not SQLite) so that
migrations, constraints, and RLS behave as they will in production.

- **Unit** — services and pure logic, with repositories faked.
- **Integration** — routers through to the database, per-test transaction
  rollback for isolation.
- **Tenant isolation suite** — cross-tenant access attempts must fail, asserted
  for every tenant-owned table.
- **Agent tests** — the Claude API is stubbed with recorded tool-use responses,
  so the loop, the policy layer and the approval gate are tested
  deterministically.

---

## 13. Deployment

Local development runs either way. `docker compose up` is the reference
environment — Postgres (pgvector image), Redis, the backend and the frontend.
The same code also runs natively against a localhost Postgres, which is what
developers without Docker use; see
[development-windows.md](development-windows.md).

Dependencies are declared as required or optional rather than assumed present.
Connections are lazy, so the application starts with an optional dependency
absent and reports it on `/health/ready` without failing the check. Redis is
optional today because nothing implemented uses it yet; it becomes required as
soon as caching, rate limiting or the job queue lands, which is a one-line
configuration change (`REDIS_REQUIRED`). Postgres is always required.

Schema changes are Alembic migrations applied on start; migrations are
forward-only and reviewed like code.

Production keeps the same shape — the container runs behind a reverse proxy with
TLS, backed by managed Postgres and Redis. Horizontal scaling is more instances
of the same image, which works because the application holds no in-process state:
sessions live in the JWT, cache and queue live in Redis, everything else is in
Postgres.

---

## 14. Key decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Overall style | Modular monolith | One deployable, one transaction boundary; module seams preserved for later extraction |
| Tenant isolation | Shared schema, `tenant_id`, RLS backstop | Simplest to operate; isolation enforced centrally and testable |
| Vector storage | `pgvector` in Postgres | One datastore; tenant filtering identical to every other query |
| Concurrency | Async SQLAlchemy and async FastAPI | The workload is I/O-bound: database, Claude API, external tools |
| Background work | Redis-backed queue | Redis is already required for cache and rate limiting |
| API versioning | `/api/v1` from the start | Cheap now, expensive to retrofit |
| Agent safety | Tool registry, policy layer, approvals | Model output is a request to act, never authority to act |
| Audit | Append-only table, no mutation | An audit log that can be edited is not an audit log |

Decisions that change materially as the project grows get an ADR in
[docs/adr/](adr/).

---

## 15. Build order

Phases mirror the roadmap in the [README](../README.md): foundation →
multi-tenancy → identity → business data → documents/RAG → agent core →
approvals → workflows → audit and monitoring → dashboard. Each phase is a
vertical slice that runs and is tested before the next one starts.
