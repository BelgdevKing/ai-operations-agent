# Evaluating this project

Written for an engineer or technical lead deciding whether this codebase is
worth their time — either to run, to build on, or to read for its architecture.

It answers the questions that decide that, in the order they usually get asked,
and it tries to be equally clear about what is missing. Everything here is
checkable against the repository; where a claim rests on a specific file, the
file is named.

---

## What is implemented

All of the following exists and is covered by tests.

| | What is actually there |
| --- | --- |
| **Agent runs** | A tool-use loop with a bounded step budget. Each run is a durable row: status, step count, tool calls, tokens, latency, stable error code. |
| **Tool framework** | A registry of typed tools with declared safety classes. Arguments are validated against a schema, a per-call timeout is enforced, and the serialised result is size-bounded. |
| **Human approval** | A tool requiring approval pauses the run and writes an approval row. An admin decides; the same run continues. The gated action runs at most once. |
| **Workflows** | Multi-step definitions — tool calls, agent steps, conditions, approval gates — run as a durable state machine that outlives the request that started it. |
| **Conversations** | Server-side conversation and message history. |
| **Multi-tenancy** | Shared schema, `organization_id` discriminator, one place where the filter is written, composite foreign keys in the database. |
| **Identity** | Argon2id passwords, JWT access tokens, organizations with owner/admin/member roles. |
| **Usage and cost** | Runs, steps, tool executions and approvals aggregated per organization, with token cost from a configurable price book. |
| **Metrics** | Prometheus text exposition, no dependency, off by default, behind a bearer token. |
| **Tracing** | Spans at six boundaries, W3C trace context, off by default. |
| **Reliability** | Every external wait bounded; dependency outages reported as `503`. |
| **Deployment** | Multi-stage production images, a separate deployed Compose stack, migrations as a one-shot job, a runbook. |
| **Console** | Next.js: sign-in, agent console, approval inbox, workflows, usage, organization members. |

**Scale of the thing:** 30 API routes, 23 tables across 7 migrations, 5 built-in
tools, 1 built-in agent, 1,781 backend and 293 frontend tests.

## What is not implemented

Stated first rather than buried, because it is what an evaluation usually turns
on.

| Missing | Detail |
| --- | --- |
| **Document retrieval (RAG)** | A `documents` table holds metadata and `app/knowledge/` is an empty package. No upload, extraction, chunking, embedding or vector search. |
| **Audit API or UI** | Audit events are written to the database. Nothing reads them back over HTTP. |
| **Row-level security** | Isolation is enforced in the repository layer and by database constraints, not by PostgreSQL RLS policies. |
| **Rate limiting and quotas** | None. Nothing bounds how much one tenant may spend. |
| **Token revocation** | An access token is valid until it expires. |
| **Invitations, password reset** | Neither exists; a second member is added directly. |
| **Billing** | None, in any form. |

Also absent, and worth saying plainly: **no security audit, no load testing, no
production deployment and no users.** The project has never run against real
traffic.

---

## Where the trust boundary is

This is the question the architecture exists to answer. The model is treated as
**untrusted input**, not as a component with authority.

```
  Language model
        │  proposes: a tool name and arguments
        ▼
  ┌─────────────────────────────────────────────┐
  │  Platform-controlled execution              │
  │                                             │
  │  1. Resolve the name in the registry        │  unknown name → refused
  │  2. Reject reserved argument names          │  identity cannot be smuggled
  │  3. Validate arguments against the schema   │  wrong shape → refused
  │  4. Check the tool is enabled and permitted │
  │  5. Approval gate, if the tool requires one │  → pauses, waits for a person
  │  6. Execute, under a timeout                │  the platform calls the tool
  │  7. Bound the result size                   │
  └─────────────────────────────────────────────┘
        │
        ▼
  Durable state: run, step, tool execution, approval, audit event
```

The properties that follow from it:

- **The model never executes anything.** It emits a structured decision naming
  a tool; `ToolExecutor` resolves, validates and runs it. There is no path from
  model output to execution that skips the validation — no `eval`, no dynamic
  dispatch on a model-supplied string beyond a registry lookup.
- **Identity cannot arrive as an argument.** Reserved argument names are
  rejected outright before schema validation, so the organization and user a
  call acts as cannot be influenced by what the model wrote.
- **A tool's own body is the last thing that runs, not the first.** Every gate
  above it is code the tool author does not control.

Read it for yourself: `backend/app/tools/executor.py` is the whole boundary, in
one file.

## How tenant isolation works

Four layers, and the point of the design is that the outer ones do not have to
be trusted.

1. **One filter.** `TenantScopedRepository` in
   `backend/app/repositories/tenant.py` is the only place `organization_id` is
   compared. A query that does not go through it does not get scoped — which is
   why every query goes through it.
2. **The database refuses cross-tenant references.** Tenant-owned rows carry a
   redundant `UNIQUE (id, organization_id)`, and a reference to another
   tenant-owned row is a composite foreign key `(child_id, organization_id) →
   parent(id, organization_id)`. A row pointing at another tenant's row is not
   rejected at runtime — it cannot be represented.
3. **The active tenant is verified, never asserted.** `X-Organization-ID`
   selects which organization a request acts on and is checked against the
   caller's membership every time. It grants nothing on its own.
4. **Telemetry carries no tenant.** No organization id reaches a metric label
   or a span attribute, so an operator watching the fleet is not thereby given
   per-tenant business data.

**What to be sceptical about:** there is no PostgreSQL row-level security. If a
future query bypassed the repository layer, layers 1 and 3 would not stop it —
only layer 2 would, and only for cross-tenant *references*. Whether that is
enough is a legitimate thing to weigh.

## How destructive operations are controlled

- A tool declares its safety class: `read_only`, `mutating` or `destructive`.
- **A destructive tool cannot opt out of approval.** The metadata model refuses
  to validate if `safety is DESTRUCTIVE and not requires_approval` — so the
  mistake is caught when the tool is defined, not when it fires.
- A gated call does not execute. The run pauses at `awaiting_approval` and an
  approval row is written.
- The approver sees an **allow-listed summary** of the action — an action name,
  a subject and named detail fields — never the raw tool arguments.
- The decision is a conditional `UPDATE` whose `WHERE` contains the current
  state and the deadline. `rowcount == 1` is the proof of having won. Two
  decisions arriving together produce one decision and one conflict, decided by
  PostgreSQL rather than by application logic.
- The execution is separately claimed the same way, so an approved action runs
  **at most once** even if the resume path is entered twice.
- An expired approval is `expired`, never `rejected` — nobody refused it — and
  the gated action never runs.

`backend/app/services/approvals.py` and
`backend/app/repositories/approval.py` are where to look.

## How usage is measured

- `GET /api/v1/ai/usage` aggregates runs, steps, tool executions and approvals
  for the caller's organization over a bounded window.
- Figures are **derived from the execution tables at read time**, not
  accumulated into a counter that could drift from what actually happened.
- Token cost comes from a versioned, model-keyed price book supplied as
  configuration. Prices are quoted as strings, money is `Decimal`, and a model
  with no configured price reports cost as **unknown** rather than zero.
- `GET /metrics` is separate, process-wide and operational: off by default,
  behind a constant-time bearer token, with bounded label sets. **No tenant id
  and no execution id is ever a label.**

The two are deliberately different things: usage is a tenant-scoped,
authenticated business question; metrics are a fleet-wide operational one.

## How failure is handled

- **Every external wait is bounded and configurable**: the model call, tool
  execution, reaching PostgreSQL, one SQL statement, the connection pool, and
  Redis. Each is bounded at both ends, so a deployment can be stricter than the
  limit but never more permissive.
- **A dependency outage is `503 service_unavailable`, not `500`.** The two are
  different operational events and a client, a load balancer and an on-call
  engineer each act differently on them. The reply carries no statement, no
  driver text, no host and no credential.
- **Only the model gateway retries**, and only transport-class failures. Nothing
  retries a tool execution, an approval, a cancellation or a workflow
  transition — each has a side effect, and repeating one is how something gets
  done twice.
- **Liveness and readiness are separate.** `/health` answers while PostgreSQL is
  down; `/health/ready` reports `degraded` and 503.
- **Start-up fails closed.** A deployed environment refuses to start on the
  shipped JWT secret, the shipped database password, `DEBUG`, or a wildcard
  CORS origin — and a configuration error never prints the value it rejected.
- **Idempotency survives all of it.** A request that fails against an
  unavailable database is rolled back before the error is raised, so it leaves
  nothing durable and the caller's `Idempotency-Key` is still free.

Details: [deployment.md](deployment.md) → *When a dependency fails*.

---

## What to test first

A short path that exercises the parts worth judging. Steps 1–3 need no model
provider credential; step 4 does.

**1. Does it stand up, and does the schema apply?**

```bash
docker compose up --build
docker compose exec backend alembic upgrade head
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/health/ready
```

**2. Does the test suite pass on your machine?**

```bash
cd backend
ruff check app tests scripts && mypy && alembic check && pytest
cd ../frontend
npm run lint && npm run typecheck && npm test && npm run build
```

Integration tests skip themselves without a reachable PostgreSQL — check the
skip count rather than only the pass count.

**3. Does the tenant boundary hold?** The interesting tests are the negative
ones. Read them before you trust them:

```bash
cd backend
pytest tests/integration/test_tenant_isolation.py -v
pytest tests/integration/test_authorization.py -v
```

**4. Does the approval gate actually stop something?** This is the demonstration
worth doing by hand. Seed the demo data, then follow
[the demo path in the README](../README.md#see-it-working-in-five-minutes):
ask the agent to cancel a shipment, watch the run stop at `awaiting_approval`,
and check that the shipment is unchanged while the decision is pending.

```bash
cd backend
python -m scripts.seed_demo_data --attach-user you@example.com
```

Then reject it, and confirm the shipment is still not cancelled.

**5. Does the telemetry leak anything?** The claim is that no tenant or
execution identifier reaches a metric or a span. It is asserted directly:

```bash
cd backend
pytest tests/integration/test_tracing_end_to_end.py -v
pytest tests/unit/test_metric_names.py tests/unit/test_tracing.py -v
```

**6. Read three files.** If the architecture is what you are evaluating, these
carry most of it:

- `backend/app/tools/executor.py` — the trust boundary, start to finish.
- `backend/app/repositories/tenant.py` — the one place the tenant filter lives.
- `backend/app/services/approvals.py` — the approval decision and the resume.

---

## Questions worth asking before adopting

Honest answers to the things that should give an evaluator pause:

**Is it production-ready?** No, and the README does not claim it is. It has no
users, no security audit and no load testing. The deployment configuration is
real and documented; it has never been under real traffic.

**How hard is it to add my own tools?** A tool is a typed schema, a metadata
declaration and an async function. The 5 built-in tools in
`backend/app/tools/business/` are the working examples, and the framework
around them does not change.

**Does it lock me into one model vendor?** No. Anthropic and OpenAI sit behind
one gateway; which vendor served a request is deliberately absent from every
durable row and every outward contract.

**What happens when the model says something wrong?** It proposes; the platform
disposes. A wrong tool name is refused, wrong arguments are refused, and a
destructive action is refused until a person allows it. What the platform does
*not* do is judge whether the model's answer is factually correct — nothing can.

**Who maintains it?** One person, unfunded, developing in public. Treat response
times accordingly; see [SECURITY.md](../SECURITY.md) for the same caveat about
vulnerability reports.

## If this is useful to your team

The platform is MIT licensed and self-hostable in full — no key, no account, no
hosted dependency. It is a reasonable foundation for an internal deployment,
custom operational tools, workflows, or an architecture review of something you
are building yourself.

See [If this is useful to your team](../README.md#if-this-is-useful-to-your-team)
for what commercial work around it could look like.

> **Commercial contact route: not configured yet.** No email address, form or
> scheduling link is published in this repository, and none has been invented
> for it. GitHub Issues is the only route that exists today.
