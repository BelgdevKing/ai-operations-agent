# Use cases

What this platform is shaped for, and what it would take to apply it.

**None of these is a customer deployment.** The project has no users. These are
worked examples of what the implemented capabilities support, written so you can
judge whether your problem is the kind of problem this architecture fits.

Each one separates three things, because conflating them is how a repository
ends up overpromising:

| | |
| --- | --- |
| **Implemented** | Exists in the repository and is covered by tests |
| **Example** | A realistic application of what is implemented, not something shipped |
| **Extension** | Work you would have to do; not present today |

The built-in tools operate on a freight and logistics dataset — shipments,
customers, charges, invoices — because the demo needed *a* domain. Nothing in
the framework is specific to it. A tool is a typed schema and an async function;
the registry, the approval gate, the run records and the tenancy do not change
when the domain does.

---

## 1. Operations assistant

**The problem.** The same questions arrive all day — where is this, what does it
cost, why is it held — and answering each one means someone opening three
internal systems. The knowledge is not scarce; the clicking is.

**What the agent does.** Answers from the systems of record through read-only
tools. It resolves a reference, pulls related records, and answers in one pass
rather than making the person assemble it.

> **Implemented.** Four read-only tools (`get_shipment`,
> `get_shipment_charges`, `get_customer`, `get_invoices`), a bounded tool-use
> loop, and a console that shows which tools were called and how each ended.

**Where approval matters.** Nowhere. Read-only tools are declared `read_only`
and execute without asking anyone — which is the point of having safety classes
at all. Gating a lookup would train people to click through the gate.

**What gets recorded.** The run with its status, step count and token usage;
every tool execution with its outcome and duration; the conversation. Cost per
organization once a price book is configured.

**Extension for your domain.** Replace the four tools with yours. The schema,
the timeout, the result bound and the recording all come from the framework.

---

## 2. Investigating a specific case

**The problem.** A customer asks why something went wrong. Answering means
reconstructing a history across records that nobody has in one place, and the
person asking is on the phone.

**What the agent does.** Follows the trail across several tools in one run —
the shipment, then its charges, then the customer's invoices — because the
loop lets it decide what to look up next based on what it just found. Each hop
is a recorded step.

> **Implemented.** Multi-step runs within a configured step budget, with
> conversation history so a follow-up question continues the same investigation
> rather than restarting it.

**Where approval matters.** Still nowhere, as long as the investigation only
reads. The moment it proposes a fix, case 3 applies.

**What gets recorded.** The full step sequence — which tool, in what order, with
what outcome and how long each took. This is the part that matters when the
question later becomes *how did we conclude that?*

**Extension.** A retrieval layer over documents and correspondence would make
investigations far stronger. It is **not implemented** — see
[evaluation.md](evaluation.md#what-is-not-implemented).

---

## 3. Controlled operational actions

**The problem.** The useful version of an operations assistant does not stop at
answering. It cancels the shipment, releases the hold, issues the credit. That
is also the version nobody is willing to deploy, because the failure mode is a
real customer's real order.

**What the agent does.** Proposes the action. It does not perform it. The
platform resolves the tool, validates the arguments against a schema, and —
because the tool is declared `destructive` — stops.

> **Implemented.** `cancel_shipment` is the worked example. A tool declared
> `destructive` **cannot opt out of approval**: the metadata refuses to validate
> if it tries, so the mistake is caught when the tool is written rather than
> when it fires.

**Where approval matters.** This is the case it exists for. The run pauses at
`awaiting_approval`; the approver sees an allow-listed summary — the action, the
subject, named detail fields — never the raw arguments the model produced. The
decision is settled by a conditional `UPDATE`, so two approvers clicking at once
produce one decision and one conflict, and the action executes **at most once**.

**What gets recorded.** Who asked, what was proposed, who decided, which way,
when, and whether the action then ran. A rejection resumes the same run and the
agent reports that nothing happened. An expired approval is `expired`, never
`rejected` — nobody refused it — and the action never runs.

**Extension.** Your destructive tools, declared the same way. The gate is the
framework's, not the tool's.

---

## 4. Human-approved automation

**The problem.** A repeatable operational procedure — five steps, a decision in
the middle, one step that needs sign-off. Today it is a runbook someone follows
by hand, and it is followed slightly differently each time.

**What the platform does.** Runs it as a workflow: a definition of tool calls,
agent steps, conditions and approval gates, executed as a durable state machine.
A run that pauses for a person survives the request that started it; the person
decides hours later and the same run continues from where it stopped.

> **Implemented.** The workflow engine, definitions with those four step types,
> durable run and step-run state, approval gates inside a workflow, and a
> `/workflows` screen showing definitions and run state.

**Where approval matters.** At the step you declare it at. A workflow can run
four steps unattended and stop at the fifth.

**What gets recorded.** The run, every step with its status and outcome, the
approval, and the decision — enough to answer *which steps ran, which were
skipped by a condition, and who allowed the one that mattered.*

**Extension.** Scheduling. There is deliberately **no job queue and no
scheduler**: a workflow run is started by a request. Something outside the
platform would have to start it on a timetable.

---

## 5. An internal agent platform for several teams

**The problem.** More than one team wants this, and their data must not mix.

**What the platform does.** Multi-tenancy is in the schema rather than bolted
on. Every tenant-owned row carries `organization_id`; one repository base class
is the only place the filter is written; composite foreign keys make a
cross-tenant reference unrepresentable in the database rather than merely
rejected. A user can belong to several organizations and chooses which one a
request acts on with a header that is checked against their membership every
time.

> **Implemented.** Tenant-scoped repositories, composite tenant-carrying foreign
> keys, organization membership with owner/admin/member roles, and negative
> tests that attempt cross-tenant access.

**Where approval matters.** Per organization. Approvers are admins of the
organization the run belongs to.

**What gets recorded.** Usage, runs and cost per organization — so *which team
is spending what* is an authenticated, tenant-scoped query rather than something
inferred from a shared metric.

**Extension.** Per-tenant rate limiting and quotas are **not implemented**.
Nothing today bounds how much one organization can spend.

---

## 6. Self-hosted, because the data cannot leave

**The problem.** The operational data is exactly the data that cannot be sent
to a third-party SaaS — or the model provider is a decision your organization
wants to keep making for itself.

**What the platform does.** Everything runs on your host, against your
PostgreSQL. The only outbound call is to the model provider you configure, and
that provider sits behind a gateway: Anthropic and OpenAI both work, and which
one served a request is deliberately absent from every durable row and every
outward contract.

> **Implemented.** A provider-independent gateway, Docker Compose for
> development and for a deployment, production images, migrations as their own
> job, and a runbook. MIT licensed with no key, account or hosted dependency.

**Where approval matters.** Unchanged — self-hosting does not alter the gate.

**What gets recorded.** The same as everywhere else, in your own database,
under your own retention policy. Metrics and tracing are both **off by default**
and carry no tenant identifier when on.

**Extension.** A model running on your own infrastructure. The gateway is the
seam where that adapter would go; only two adapters exist today.

---

## Where this fits badly

Worth being direct about, so you can rule it out quickly:

- **Open-ended chat.** There is no retrieval, and the agent can only answer what
  a tool can fetch. A general assistant over your documents is the thing that is
  **not built**.
- **High-volume autonomous actions.** The design assumes a human in the loop for
  anything destructive. If you want thousands of unattended mutations per hour,
  the approval gate is friction rather than safety — and there is no rate
  limiting to protect you either.
- **Sub-second latency.** A run is a loop of model calls. It is built for
  correctness and traceability, not for responding inside a page load.
- **A drop-in product.** The built-in tools operate on a demo dataset. Real use
  means writing tools against your systems.

## Trying one of these

The demo path in the [README](../README.md#see-it-working-in-five-minutes)
walks cases 1 and 3 end to end with seeded data: ask a question, get an answer
from two tools, then ask for a cancellation and watch the run stop for approval.

For judging whether the architecture holds, [evaluation.md](evaluation.md) is
the more useful document.
