# Service brief

The technical basis for a conversation about building something on this
architecture.

> **Status: nothing here is currently offered or sold.** There are no clients,
> no engagements, no published rates, no hosted service and no contact route. A
> route has to be configured by the repository owner before any of this can be
> acted on — see [Current status](#current-status).

This exists so that a technically serious reader can judge *what such work would
involve* rather than guess. It is a scope document, not an advertisement.

---

## What could be built on this

The repository is the architectural half of an internal AI operations system:
the agent loop, the tool boundary, the approval gate, the tenancy, the durable
execution records and the observability. What it does not contain — and cannot —
is anyone's actual systems.

Systems that could be built on it:

- **An operations assistant** over internal systems of record, answering the
  questions a team answers by hand, with read-only tools.
- **Controlled operational actions** — cancel, release, credit, reschedule —
  where each destructive action stops for a named human and the decision is
  durable.
- **Human-approved automation**: a repeatable procedure as a workflow, running
  unattended up to the step that needs sign-off.
- **A shared internal agent platform** for several teams, with data separated at
  the schema level rather than by convention.
- **An auditable execution trail** for actions taken on a customer's behalf.

What each has in common: the value is in *actions* being controlled. For pure
question-answering this architecture is more structure than the problem needs,
and [client-problems.md](client-problems.md#where-this-is-the-wrong-tool) says
so.

## Typical starting point

A small discovery and architecture exercise, followed by a focused proof of
concept.

**Discovery** establishes which systems are involved, which actions matter, and
which of those are destructive — the last being the question that shapes
everything else. Its legitimate output includes *"this architecture does not
fit"*.

**A proof of concept** is deliberately narrow: one real read-only tool and one
real destructive tool against a real system, with the approval gate working
end to end. That is the demonstration in [demo.md](demo.md) with the client's
data in place of the freight dataset, and it is enough to tell whether the
approach survives contact with their environment.

Neither step commits anyone to what follows.
[revenue-path.md](revenue-path.md#a-possible-engagement-progression) sketches
the full progression — proposed, never run.

## Example technical scope

The concrete work items, each corresponding to a real extension point. The
source files are named in [extensions.md](extensions.md).

| Work item | What it involves |
| --- | --- |
| **Connect an internal system** | Repository access to the system of record, behind the tenant-scoped repository pattern |
| **Define read-only tools** | Typed input and output models, a `read_only` safety class, registration |
| **Define destructive tools** | The same, declared `destructive` — which makes approval mandatory rather than optional |
| **Add approval requirements** | The allow-listed summary a person is shown, and which role may decide |
| **Connect an LLM provider** | Anthropic or OpenAI work today; another provider or a self-hosted model is an adapter behind the existing gateway |
| **Implement workflows** | Multi-step definitions with conditions and approval gates, as documents rather than code |
| **Add tenant-aware persistence** | New tables carrying `organization_id`, composite foreign keys, and a migration |
| **Add observability** | Metrics and tracing exist and are off by default; wiring them to a collector and dashboards |
| **Deploy into the environment** | The production Compose stack adapted to the client's infrastructure |
| **Console work** | Screens for the specific operational context |

## What is not included automatically

Every deployment differs in ways the repository cannot anticipate. Each of the
following needs separate assessment, and none is covered by the architecture
being sound:

- **Authentication environment.** The platform has its own accounts and JWTs.
  Integrating an existing identity provider, SSO or directory is work, and the
  platform has **no token revocation** today.
- **Cloud infrastructure.** The deployment target is Docker Compose on one host.
  A managed container platform, managed PostgreSQL and a secret manager are an
  adaptation, not a configuration change.
- **Internal APIs.** Their shape, their auth, their rate limits and their
  reliability all determine how tools are written and how they fail.
- **Security requirements.** Threat model, perimeter, network policy, key
  management. The repository documents its own boundaries in
  [evaluation.md](evaluation.md); it cannot assume yours.
- **Compliance requirements.** Audit reporting, retention, residency, right to
  erasure. Note that audit events are *recorded* but **no audit API or export
  exists** — reporting would be built.
- **Data model.** Which entities exist, what a tenant owns, what a destructive
  action means in that domain.
- **Operational requirements.** Availability expectations, backup and restore,
  monitoring, on-call, incident response. The platform has **no rate limiting,
  quotas or abuse controls**; anything beyond an internal perimeter needs them
  built.

[adoption.md](adoption.md) is the fuller version of this division of labour, and
is deliberately blunt about it.

## What the repository already gives you

So the scope above is judged against the right baseline — these do **not** need
building:

- The proposal/execution split and its eight validation gates.
- The approval gate, including at-most-once execution and expiry.
- Tenant isolation at the schema level, with database-enforced constraints.
- Durable run, step, tool-execution and approval records.
- Usage and cost attribution per organization.
- Metrics, tracing, bounded timeouts and dependency-failure handling.
- A console covering the agent, approvals, workflows and usage.
- Production images, a deployed stack, migrations as a job, and a runbook.
- 1,781 backend and 293 frontend tests over those boundaries.

## Current status

Stated plainly, because a brief that implies more than exists is worse than none:

| | |
| --- | --- |
| Clients or engagements | **None.** No work has been performed for anyone |
| Published rates or pricing | **None**, anywhere in this repository |
| Hosted service | **None**, and none planned — [commercial.md](commercial.md#a-hosted-offering) |
| Support contract, SLA, warranty | **None.** The MIT licence's "as is" is the whole of it |
| Contact route | **Not configured yet** |
| Production deployments of this software | **None known.** It has never run under real traffic |

> **A contact route still needs to be configured by the repository owner.** No
> email address, contact form or scheduling link is published here, and none has
> been invented. GitHub Issues is the only route that exists today. See
> [owner-actions.md](owner-actions.md#revenue-activation).

The software itself is MIT licensed and free to use, modify and deploy
commercially, with or without any of the above. Nothing on this page is a
condition of using it.

---

**Related:** [technical-overview.md](technical-overview.md) — what the
architecture is · [client-problems.md](client-problems.md) — the problems it
addresses · [evaluation.md](evaluation.md) — how to verify the claims ·
[revenue-path.md](revenue-path.md) — how this could become paid work.
