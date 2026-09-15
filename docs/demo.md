# Running the demo

A practical guide for someone who wants to see the central idea working rather
than read about it:

> **The model proposes. The platform disposes.**

The demonstration is deliberately small. An agent answers an operational
question by calling two read-only tools. Then it is asked to cancel something —
and the run **stops**, because cancelling is declared destructive and no model
output can authorise it. A person decides; the run continues either way; and
what happened is durable.

Roughly fifteen minutes end to end, most of it waiting for `pip install`.

---

## What you need

| | |
| --- | --- |
| **Docker path** | Docker and Docker Compose. Nothing else. |
| **Native path** | Python 3.12, Node.js 18.18+, and a PostgreSQL you can connect to. |
| **A model provider key** | Anthropic or OpenAI. **Required only for steps 4–7.** |

**What works without a provider key:** installing, migrating, seeding the demo
data, registering, signing in, the whole console, the approvals screen, the
usage report, metrics, tracing, and the entire test suite.

**What needs one:** actually running an agent — which is steps 4 to 7, the part
worth seeing. Without a key the platform starts normally and a run fails with
`llm_configuration_error`, deliberately, rather than failing at start-up.

If you have no key, [Verifying it without a provider key](#verifying-it-without-a-provider-key)
at the end shows the same guarantees through the test suite instead.

---

## 1. Start it

### Docker

```bash
git clone https://github.com/BelgdevKing/ai-operations-agent.git
cd ai-operations-agent
docker compose up --build
docker compose exec backend alembic upgrade head
```

### Native

Backend, in one terminal:

```bash
cd backend
py -3.12 -m venv .venv          # python3 -m venv .venv on macOS/Linux
.venv\Scripts\activate          # source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then set DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload
```

Frontend, in a second:

```bash
cd frontend
npm install
npm run dev
```

### Add your provider key

For steps 4–7. Set it in `backend/.env` (native) or `.env` (Docker), then
restart the backend:

```
ANTHROPIC_API_KEY=your-key-here
```

Or `LLM_PROVIDER=openai` with `OPENAI_API_KEY`. Only the selected provider's
key is needed.

### Check it is up

```bash
curl -fsS http://localhost:8000/health         # the process
curl -fsS http://localhost:8000/health/ready   # its dependencies
```

`/health` answers even when PostgreSQL is down — that is the point of the
split. `/health/ready` reports `degraded` with a 503 when a required dependency
is unreachable.

## 2. Create an account

At http://localhost:3000/register, or over the API:

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-password",
       "organization_name":"Acme Operations"}'
```

Registration creates the user, an organization, and an **owner** membership for
you — which matters later, because approving is an admin action.

It returns the user and organization but **not** a token. Sign in for that:

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-password"}'
```

Keep the `access_token` for the API checks below:

```bash
TOKEN=...   # the access_token from the response
```

## 3. Load the demo data

```bash
cd backend
python -m scripts.seed_demo_data --attach-user you@example.com
```

This writes two demo organizations — Northwind Freight and Globex Logistics —
with customers, shipments, charges and invoices, and attaches your account to
both as an owner so you can see the data.

It is idempotent: every row's id is derived from its business key, so running it
twice refreshes the same rows rather than creating a second copy. It refuses to
run unless `APP_ENV=development`.

It prints the question to start with.

> **A detail worth noticing.** Shipment `ABC123` exists in *both* demo
> organizations, with different statuses. That duplication is deliberate: it is
> there so tenant isolation is testable against a reference that is genuinely
> ambiguous without the tenant scope.

---

## The scenario

```
  You ask an operational question
            ↓
  Agent calls permitted read-only tools          get_shipment, get_shipment_charges
            ↓
  You ask for a destructive action               "cancel shipment ABC123"
            ↓
  The agent proposes; the platform refuses to act
            ↓
  Run status: awaiting_approval                  nothing has been cancelled
            ↓
  A human reviews an allow-listed summary        not the raw tool arguments
            ↓
      approve                 reject
            ↓                    ↓
  Cancelled exactly once   Nothing cancelled
            ↓                    ↓
  Run resumes and reports either way; both decisions are durable
```

## 4. Ask a question

Open http://localhost:3000/ai, select **Operations assistant**, and ask:

> Check shipment ABC123 and tell me if there are outstanding charges.

**What to watch.** The execution panel shows the run's status, step count, tool
calls, tokens and model time. The tool activity list shows `get_shipment` and
`get_shipment_charges`, each with its outcome.

**What it demonstrates.** The model did not query a database. It emitted two
tool names; the platform resolved them in its registry, validated the arguments
against a schema, executed them itself under a timeout, and bounded the results.
Both tools are declared `read_only`, so no approval was required — gating a
lookup would only teach people to click through the gate.

## 5. Ask for something destructive

> Cancel shipment ABC123.

**What to watch.** The run does not complete. Its status becomes
**`awaiting_approval`** and the console shows an approval panel instead of an
answer.

**What it demonstrates.** `cancel_shipment` is declared `destructive`, and a
destructive tool **cannot opt out of approval** — the tool metadata refuses to
validate if it tries, so the mistake is impossible to make at definition time
rather than caught at execution time.

Nothing has been cancelled. The shipment is untouched while the decision is
pending, and you can prove that before deciding — see step 6.

## 6. Verify that nothing happened yet

Before approving, confirm the platform has not acted.

**The approval exists and is pending:**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/approvals
```

Returns `{"approvals": [...], "next_cursor": null}`. The pending entry names the
action and its subject.

**The run is paused, not failed:**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/ai/runs
```

The run's `status` is `awaiting_approval`.

**The shipment is unchanged**, checked in the database rather than through the
application that is making the claim:

```bash
psql "$DATABASE_URL" -c \
  "SELECT reference, status FROM shipments WHERE reference = 'ABC123';"
```

Two rows, one per demo organization — and neither is cancelled.

**What the approver sees.** Open http://localhost:3000/approvals. The entry
shows the action, the subject and an allow-listed set of detail fields. It does
**not** show the raw tool arguments: what the model wrote is not what a person
is asked to rubber-stamp.

## 7. Decide

In the console, or over the API. Both require the **admin** role — an owner
qualifies; a plain member does not.

```bash
# Approve
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/approvals/<approval-id>/approve

# or reject
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/approvals/<approval-id>/reject
```

**If you approve.** The same run resumes, the tool executes once, and the agent
reports what it did. Check the database again — the shipment in *your*
organization is now cancelled, and the other organization's `ABC123` is not.

**If you reject.** The same run also resumes, and the agent reports that nothing
was cancelled. A rejection is a decision that gets an answer, not an error.

**Either way**, the decision, who made it and when are durable rows. An approval
that nobody answers becomes `expired`, never `rejected` — nobody refused it —
and the gated action never runs.

**Try it twice.** Decide the same approval again. The second attempt is a
conflict, not a second cancellation: the decision is a conditional `UPDATE`
whose `WHERE` clause contains the current state, so PostgreSQL settles the race
rather than application logic.

## 8. See what it cost

http://localhost:3000/dashboard, or:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/ai/usage
```

Agent runs, tool executions, approvals, token counts and cost for **your**
organization over a bounded window — derived from the execution tables at read
time, not accumulated in a counter that could drift from what happened.

Cost reports as **unknown** until `LLM_PRICING` is configured. That is
deliberate: a model with no configured price reports honestly rather than
reporting zero.

## 9. Optional — watch the machinery

Set `TRACING_ENABLED=true` and restart the backend. One request now produces a
span tree in the log: the HTTP request, the agent run, each model call, each
tool execution. Every span carries a route template and a lifecycle state, and
no tenant id, execution id, prompt, tool argument or result.

Set `METRICS_ENABLED=true` with a `METRICS_TOKEN` and `/metrics` serves
Prometheus text behind a bearer token. There is no unauthenticated mode; enabling
metrics without a token refuses to start.

---

## Verifying it without a provider key

Every guarantee the demo shows is asserted by tests that need no credential —
they drive the real runtime, the real tool framework and the real approval
service behind a scripted model.

```bash
cd backend

# The approval gate: a destructive tool pauses; the shipment is not cancelled
# while pending; approving cancels it exactly once; rejecting cancels nothing.
pytest tests/integration/test_approval_workflow.py -v

# Tenant isolation, including cross-tenant access attempts.
pytest tests/integration/test_tenant_isolation.py tests/integration/test_authorization.py -v

# Telemetry carries no tenant or execution identifier.
pytest tests/integration/test_tracing_end_to_end.py -v
```

These need a reachable PostgreSQL. Without one they **skip themselves rather
than fail**, so check the skip count, not just the pass count.

---

## Troubleshooting

Only problems this repository actually produces.

| Symptom | Cause |
| --- | --- |
| The run fails with `llm_configuration_error` | No provider key for the selected `LLM_PROVIDER`. Steps 1–3, 6 and 8 still work without one. |
| No agent in the picker | Not signed in, or the backend is unreachable. The built-in **Operations assistant** needs no seeding. |
| The agent answers instead of pausing | It chose a read-only tool. The gate is on the tool, not on the phrasing — ask it to *cancel* a shipment specifically. |
| Approve returns `403 permission_denied` | Deciding requires the **admin** role. Registration makes you an owner of your own organization, which qualifies. |
| Approve returns `409` on a second attempt | Correct. The first decision won; the second is a conflict, not a second cancellation. |
| Approve returns `410` | The approval expired. It is `expired`, never `rejected`, and the action never runs. |
| Seeder prints `Refusing to seed demo data in APP_ENV=...` | It only runs with `APP_ENV=development`. |
| The shipment is already cancelled | A previous run of the demo. Re-run the seeder; it refreshes the same rows. |
| Cost shows as unknown | `LLM_PRICING` is empty, or has no entry for that model. Usage itself is still reported in full. |
| API returns `503 service_unavailable` | A dependency is down, almost always PostgreSQL. Check `/health/ready`. Not an application fault. |
| Run stuck in `running` after a restart | Expected. There is no background worker; the abandonment sweep fails it after `AGENT_RUN_STALE_AFTER_SECONDS`. |
| `/metrics` returns 404 | `METRICS_ENABLED` is false, which is the default. |

Setup problems specific to a native Windows install — PostgreSQL on `PATH`,
execution policy, role passwords — are in
[development-windows.md](development-windows.md#9-troubleshooting).

---

## Where to go next

- [evaluation.md](evaluation.md) — what is and is not implemented, where the
  trust boundary is, and what to test first.
- [use-cases.md](use-cases.md) — whether this architecture fits your problem.
- [architecture.md](architecture.md) — the design and the reasoning.
- `backend/app/tools/executor.py` — the boundary this demo exists to show, in
  one readable file.
