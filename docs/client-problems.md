# Problems this architecture addresses

Organised by **problem**, not by industry or by feature.

The companion document [use-cases.md](use-cases.md) asks *"what could I build
with this?"* This one asks the question a team usually arrives with: *"we have
this specific difficulty — does any of this help?"* Several of these are the
problems a team hits **after** an AI prototype already works, which is the point
at which this architecture starts to matter.

Every entry states what the repository actually does about it, and where it
stops. **No customer story here is real. There are no customers.**

| | |
| --- | --- |
| ✅ **Addressed** | Implemented and covered by tests |
| ⚠️ **Partly** | The mechanism exists; the rest is deployment or custom work |
| ⛔ **Not addressed** | Would be custom work or is not built |

---

## 1. "Our AI can answer questions but we cannot let it *do* anything"

**The problem.** The prototype works. It answers questions well. Nobody will
approve letting it touch a real system, because the failure mode is a real
customer's real order, and nobody can articulate what stops it.

**What this does.** It separates proposing from executing, structurally. The
model emits a tool name and arguments; the platform resolves, validates,
authorizes, executes and records. A tool declared `destructive` cannot execute
unattended — the metadata refuses to validate if it tries, so the guarantee is
enforced when the tool is written rather than when it fires.

The answer to "what stops it" becomes one file somebody can read:
[`backend/app/tools/executor.py`](../backend/app/tools/executor.py).

✅ **Addressed.** ⛔ **Not addressed:** deciding *which* of your actions are
destructive. That judgement is yours, and it is the part worth spending time on.

## 2. "The model decides *and* executes, and we cannot tell them apart"

**The problem.** A prototype where the model calls functions directly. There is
no point where you could insert a check, because proposing and executing are the
same step. Adding safety means restructuring, and nobody is sure into what.

**What this does.** This *is* the restructuring, already done. Model output is
data — a name and arguments — validated before anything runs. Eight gates sit
between the model and any effect, and none of them is code the tool author
controls.

✅ **Addressed** as a reference implementation. ⚠️ **Partly** for your codebase:
adopting the shape means moving your functions behind the tool interface, which
is a real port rather than a drop-in — [extensions.md](extensions.md#1-a-read-only-tool)
shows the size of it.

## 3. "We need a person to approve high-impact actions, and 'we'll add that later' never happened"

**The problem.** Everyone agrees there should be a human in the loop. Nobody has
built it, because doing it properly means a durable pause, a queue, a decision
that survives a restart, and a guarantee the action runs exactly once.

**What this does.** All four, as the default path rather than an addition. The
run pauses at `awaiting_approval` and the state is a database row. The approver
sees an allow-listed summary — an action, a subject, named fields — never the
raw arguments. The decision is a conditional `UPDATE`, so two approvers clicking
at once produce one decision and one conflict, settled by PostgreSQL. The
execution is claimed the same way, so an approved action runs **at most once**.
An approval nobody answers becomes `expired`, never `rejected`, and the action
never runs.

✅ **Addressed.** ⛔ **Not addressed:** notifying the approver. There is no
email, no Slack, no push — the inbox is a screen someone opens. Wiring a
notification is custom work.

## 4. "Answering one question means opening four systems"

**The problem.** The knowledge is not scarce; the clicking is. The same
operational questions arrive all day and each one costs someone several minutes
of navigation.

**What this does.** Read-only tools against the systems of record, and a loop
that can decide what to look up next based on what it just found — so one
question can span several lookups in one pass.

⚠️ **Partly.** The framework is complete and five worked tools ship against a
demo dataset. **Tools against *your* systems are the work**, and that is the
most common shape a first engagement would take.

## 5. "We need an auditable trail of what the AI did"

**The problem.** Someone will eventually ask why an order was cancelled, and
"the assistant did it" is not an answer that survives the question.

**What this does.** Every run, step, tool execution, approval and decision is a
durable row: what was proposed, who decided, which way, when, and whether the
action then ran. Audit events are written for the significant transitions.
Correlation ids tie an API response to its server log line.

⚠️ **Partly — and be clear about the gap.** The trail is *recorded* but **there
is no audit API and no audit screen**; nothing reads it back over HTTP. If you
owe a regulator or a customer a report, the reporting layer is yours to build.
Retention and deletion are also yours — nothing expires.

## 6. "Our AI prototype has no tenant isolation and we now have a second customer"

**The problem.** The prototype assumed one organization. Adding a second means
auditing every query, and the failure mode is one customer seeing another's
data.

**What this does.** Multi-tenancy in the schema rather than bolted on. Every
tenant-owned row carries `organization_id`; one repository base class is the
only place the filter is written; and references between tenant-owned rows are
composite foreign keys, so a cross-tenant reference is **unrepresentable**
rather than merely rejected. The active organization arrives in a header that is
verified against membership on every request.

✅ **Addressed**, with one honest caveat: there is **no PostgreSQL row-level
security**. A query bypassing the repository layer would be caught only by the
database constraints, and only for cross-tenant *references*. Whether that is
enough is a legitimate thing to weigh — [evaluation.md](evaluation.md#how-tenant-isolation-works)
sets it out.

## 7. "We cannot tell what the AI is costing us, per team"

**The problem.** One provider bill, several teams, and no way to attribute it.

**What this does.** Runs, steps, tool executions, approvals and tokens
aggregated per organization, derived from the execution tables at read time
rather than from a counter that can drift. Cost comes from a configurable
price book; an unpriced model reports *unknown* rather than zero.

✅ **Addressed** for attribution. ⛔ **Not addressed:** there are **no quotas and
no rate limiting**. Nothing stops a tenant spending; you can see what was spent,
not cap it.

## 8. "Moving from a demo to something we would actually run internally"

**The problem.** The demo runs on a laptop. Running it internally raises
questions the demo never had to answer — secrets, migrations, health checks,
what happens when the database is down, what to do on a bad deploy.

**What this does.** Production images separate from development ones, a deployed
Compose stack that refuses to start on any published default, migrations as
their own one-shot job, liveness and readiness that mean different things,
bounded timeouts on every external wait, dependency outages reported as `503`
rather than `500`, and a runbook covering rollback and backups.

⚠️ **Partly.** The mechanics are there and documented. **Your environment is
not** — identity provider, cloud, secret manager, monitoring, backup schedule
and the operational ownership are all yours. [adoption.md](adoption.md) is the
division of labour.

## 9. "We want this on our own infrastructure"

**The problem.** The operational data is exactly the data that cannot go to a
third-party SaaS, or the model vendor is a decision you want to keep making.

**What this does.** MIT licensed, self-hostable in full, no key, no account, no
hosted dependency, no phone-home. One outbound call, to the provider you
configure. Anthropic and OpenAI sit behind one gateway and a self-hosted model
is an adapter away.

✅ **Addressed.** ⛔ **Not addressed:** a hosted service — there is none, and
none is planned. [commercial.md](commercial.md#a-hosted-offering) says why.

---

## Where this is the wrong tool

Worth ruling out quickly:

- **Open-ended chat over documents.** Retrieval is not built. If that is the
  requirement, this is the wrong starting point.
- **High-volume unattended actions.** The design assumes a person in the loop
  for anything destructive; at thousands of unattended mutations an hour the
  gate is friction, and there is no rate limiting to protect you either.
- **Sub-second latency.** A run is a loop of model calls. Built for correctness
  and traceability, not for a page load.
- **A public-facing product, today.** No rate limiting, no abuse controls, no
  account lifecycle. Ready behind your own perimeter; not ready as a public
  service.

## If one of these is your problem

[demo.md](demo.md) shows problems 1 and 3 working end to end in about fifteen
minutes. [evaluation.md](evaluation.md) is how to check the claims rather than
take them. [service-brief.md](service-brief.md) describes what engineering work
around this could look like — and states plainly that no such work is currently
offered or sold.
