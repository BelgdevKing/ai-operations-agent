# Technical overview

A single read for someone deciding whether this architecture is worth a
conversation. [architecture.md](architecture.md) is the design document with the
reasoning; this is the shape of the thing in one sitting.

---

## The idea

> **The model proposes. The platform disposes.**

A language model is treated as untrusted input, not as a component with
authority. Its entire output is *a tool name and some arguments*. Everything
that happens next is the platform's, and none of it can be skipped by anything
the model writes.

Concretely, when a model asks to cancel a shipment, this is what runs — in
[`backend/app/tools/executor.py`](../backend/app/tools/executor.py), one file:

| Step | What it does | If it fails |
| --- | --- | --- |
| 1 | Resolve the name in the tool registry | Unknown name → refused |
| 2 | Reject reserved argument names | Identity cannot arrive as an argument |
| 3 | Validate arguments against the tool's schema | Wrong shape → refused |
| 4 | Check the tool is enabled and permitted here | Not permitted → refused |
| 5 | **Check the safety class** | `destructive` → the run pauses for a person |
| 6 | Execute the tool, under a timeout | Deadline is never waived |
| 7 | Bound the serialised result | Oversized → refused, not truncated |
| 8 | Record the execution | Durable row, whatever the outcome |

The model never holds a credential, a database connection or the ability to
call anything directly.

## Request flow

```
Person → Console → API → Agent runtime → Model gateway → Model
                              ↓                ↑
                              └── proposes a tool ┘
                              ↓
                        Tool executor
                              ↓
                   ┌──── safety class ────┐
              read_only               destructive
                  ↓                        ↓
             execute now            run pauses at
                  ↓                 awaiting_approval
                  ↓                        ↓
                  ↓               admin approves/rejects
                  ↓                        ↓
                  └────────┬───────────────┘
                           ↓
          PostgreSQL: run, steps, tool executions,
          approvals, conversations, audit events
                           ↓
            usage · cost · metrics · traces
```

## The pieces

### Agent runtime — `backend/app/agents/`

A tool-use loop with a bounded step budget. Each run is a durable row carrying
status, step count, tool calls, tokens, latency and a stable error code. A run
that pauses for approval survives the request that started it: the person can
decide hours later and the same run continues.

There is **no background worker**. A run is advanced by the request that asked
for it, which is why a run whose request died is swept and failed rather than
silently resumed.

### Tool registry and safety classes — `backend/app/tools/`

Tools are typed: a Pydantic input model, an output model, and metadata
declaring a safety class.

| Class | Meaning |
| --- | --- |
| `read_only` | Looks something up. Running it twice changes nothing. |
| `mutating` | Changes state or has an outside effect. |
| `destructive` | Removes or cancels something. Hard or impossible to undo. |

**A `destructive` tool cannot opt out of approval.** The metadata model refuses
to validate if it tries, so the mistake is caught when the tool is written
rather than when it fires.

Five tools ship as worked examples — four read-only, one destructive — against a
freight dataset. Nothing in the framework is specific to that domain.

### Approval boundary — `backend/app/services/approvals.py`

A gated call does not execute. The run's status becomes `awaiting_approval` and
an approval row is written. The approver sees an **allow-listed summary** — an
action, a subject, named detail fields — never the raw arguments the model
produced.

The decision is a conditional `UPDATE` whose `WHERE` clause contains the current
state and the deadline; `rowcount == 1` is the proof of having won. Two
approvers deciding at once produce one decision and one conflict, settled by
PostgreSQL rather than by application logic. The execution is claimed the same
way, so an approved action runs **at most once**.

An approval nobody answers becomes `expired`, never `rejected` — nobody refused
it — and the gated action never runs.

### Workflow engine — `backend/app/workflows/`

Multi-step definitions executed as a durable state machine. Four step types:
`tool_call`, `agent_step`, `condition`, `approval`. Definitions are validated at
activation — acyclic, bounded in size and step count — and a run that pauses at
an approval step resumes the same way an agent run does.

Workflows are data, not code: a definition is a JSON document posted to the API.

### Tenant isolation — `backend/app/repositories/tenant.py`

Shared schema with an `organization_id` discriminator, and four layers:

1. `TenantScopedRepository` is the **only** place the filter is written.
2. Tenant-owned rows carry a redundant `UNIQUE (id, organization_id)`, and
   references between them are composite foreign keys — a cross-tenant
   reference is **unrepresentable**, not merely rejected.
3. `X-Organization-ID` selects which organization a request acts on and is
   checked against the caller's membership every time. It grants nothing.
4. No tenant identifier reaches a metric label or a span attribute.

There is **no PostgreSQL row-level security**; layers 1 and 3 are application
code, and layer 2 is the database backstop.

### Authentication and authorization

Argon2id passwords above the OWASP minimum. JWT access tokens restricted to
HMAC algorithms, so algorithm confusion is refused by configuration rather than
by validation. Organizations with owner / admin / member roles; approving is an
admin action. Tokens are held in browser memory only — nothing in
`localStorage`, `sessionStorage`, cookies or IndexedDB.

There is **no token revocation**: expiry is what ends a session.

### Persistence

PostgreSQL, 23 tables across 7 migrations. Operational tables hold ids, counts,
codes and timings; conversation content lives separately from them. Every
tenant-owned table carries `organization_id`.

### Usage and cost — `backend/app/services/usage.py`

Runs, steps, tool executions and approvals aggregated per organization over a
bounded window, **derived from the execution tables at read time** rather than
accumulated into a counter that can drift from what happened. Token cost comes
from a versioned, model-keyed price book supplied as configuration; money is
`Decimal` and a model with no configured price reports cost as *unknown* rather
than zero.

### Observability — `backend/app/observability/`

Both off by default.

- **Metrics**: Prometheus text exposition, no third-party dependency, behind a
  constant-time bearer token. Bounded label sets with an overflow bucket. No
  tenant id and no execution id is ever a label.
- **Tracing**: spans at six boundaries with W3C trace-context propagation. A
  23-key attribute allow-list, and any UUID-shaped value is dropped whatever key
  it arrives under. A failing exporter cannot fail a request.

### Reliability

Every external wait is bounded and configurable — the model call, tool
execution, reaching PostgreSQL, one SQL statement, the connection pool, Redis —
each bounded at both ends, so a deployment can be stricter but never more
permissive.

A dependency outage answers `503 service_unavailable`, not `500`, because a
client and an on-call engineer act differently on the two. Only the model
gateway retries, and only transport failures; nothing retries a tool execution,
an approval, a cancellation or a workflow transition.

### Console — `frontend/`

Next.js 15 / React 19 / TypeScript. Sign-in, agent console with live execution
detail and tool activity, approval inbox, workflows, usage, organization
members. One API client; no ad-hoc `fetch`. Model output renders as plain text,
never as markup.

### Deployment

Docker Compose, with development and production as separate stacks rather than
an overlay. Multi-stage images, migrations as their own one-shot job, no host
ports on the database, and a deployed environment that refuses to start on any
secret this repository publishes. [deployment.md](deployment.md) is the runbook.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.x async · PostgreSQL 17 · Pydantic v2 ·
Alembic · Next.js 15 · React 19 · TypeScript · Tailwind CSS v4 · Anthropic and
OpenAI behind one gateway · Docker Compose · pytest and Node's built-in test
runner.

**1,781 backend and 293 frontend tests.** Written against boundaries — tenant
isolation, the approval gate, telemetry leakage — rather than for coverage.

## What is not built

Named here because an evaluation usually turns on it:

**Retrieval (RAG)** — a `documents` table holds metadata and `app/knowledge/` is
an empty package; no upload, extraction, chunking, embedding or vector search.
**An audit API** — events are written; nothing reads them back over HTTP.
**Row-level security. Rate limiting and quotas. Token revocation. Invitations.
Password reset. Billing.**

Also absent: any security audit, load testing, production deployment or users.
The platform runs end to end locally and in a Compose deployment; it has never
been under real traffic, and nothing here claims it is production-ready for
arbitrary public exposure. [adoption.md](adoption.md) sets out what a deployment
still owes.

---

**Next:** [evaluation.md](evaluation.md) to check these claims ·
[demo.md](demo.md) to watch the approval boundary work ·
[extensions.md](extensions.md) to see where your own work plugs in ·
[architecture.md](architecture.md) for the full design and its reasoning.
