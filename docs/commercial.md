# Commercial use

What you can do with this repository commercially, what exists today, and what
does not.

This is not a sales page. There is nothing to buy.

---

## What exists today

The repository is **MIT licensed and self-hostable in full**. No key, no
account, no hosted dependency, no phone-home, no usage reporting. You can run it
for a customer, embed it in a product, fork it and never mention it again — the
licence permits all of that.

Everything below is implemented and covered by tests. The
[evaluation guide](evaluation.md) says how to check each one.

| | |
| --- | --- |
| **Agent runtime** | A tool-use loop with a bounded step budget; every run a durable row with status, steps, tokens, latency and a stable error code |
| **Permissioned tools** | A registry of typed tools with declared safety classes; arguments validated against a schema, a per-call timeout, a bounded result |
| **Human approval** | Destructive tools cannot execute unattended; the gate is enforced at tool definition and settled by a conditional `UPDATE` |
| **Workflows** | Multi-step definitions run as a durable state machine, with approval gates at any step |
| **Multi-tenancy** | `organization_id` on every tenant-owned row, one place where the filter is written, composite foreign keys in the database |
| **Usage and cost** | Per-organization, derived from the execution tables, with a configurable price book |
| **Observability** | Prometheus metrics and W3C-propagated traces, both off by default, neither carrying a tenant or execution identifier |
| **Reliability** | Every external wait bounded; dependency outages reported as `503`; only the model gateway retries |
| **Console** | A Next.js app: agent console, approval inbox, workflows, usage, members |
| **Deployment** | Multi-stage production images, a separate deployed Compose stack, migrations as their own job, a runbook |

### What does not exist

Stated here as well as in the README, because it is what a commercial
conversation would turn on:

- **No hosted service.** Nothing is running anywhere. There is no sign-up, no
  trial, no multi-tenant SaaS operated by anyone.
- **No paid tier and no subscription.** The MIT licence covers everything in the
  repository; there is no second, commercial edition and nothing is billed.
- **No support contract, SLA or warranty.** The licence's "as is" is the whole
  of it.
- **No formal consulting package, statement of work or published rate.** The
  services below are possibilities, not an offering with terms attached.
- **No retrieval (RAG), audit API, row-level security, rate limiting, quotas,
  token revocation, invitations or password reset.** See
  [evaluation.md](evaluation.md#what-is-not-implemented).
- **No security audit, no load testing, no production deployment, no users.**

## Potential professional services

**None of these is currently offered or sold.** No engagement has been
performed, no rate has been set, and no client exists. They are listed because
they are the work this codebase would realistically be a starting point for, and
because a reader deciding whether to invest time in it is entitled to know
whether help could ever be available.

If any becomes a real offering, it will be stated here as one.

Five categories rather than a longer list, because these are the shapes the work
actually takes. Each names where in the repository it would begin, so the scope
is judged against real code rather than a description.

### Architecture review

The proposal/execution split, the tenant boundary, approval semantics, agent
safety, observability and reliability — for a team building something
comparable, or evaluating what they have already built.

Begins at [`backend/app/tools/executor.py`](../backend/app/tools/executor.py)
and [`backend/app/repositories/tenant.py`](../backend/app/repositories/tenant.py).
Its legitimate output includes *"this architecture does not fit"*.

### Proof of concept

One real read-only tool and one real destructive tool against a real system,
with the approval gate working end to end — the demonstration in
[demo.md](demo.md) with a client's data in place of the freight dataset.

Deliberately narrow, because it is enough to tell whether the approach survives
contact with an actual environment. Extension points 1 and 2 in
[extensions.md](extensions.md#1-a-read-only-tool).

### Custom integration

Connecting the platform to the systems it must read and act on: repository
access behind the tenant-scoped pattern, typed tools with the safety classes and
approval policy that match the risk, operational procedures as workflows, and a
different model provider or a self-hosted model behind the existing gateway.

Extension points 1, 2, 4, 5 and 7 in [extensions.md](extensions.md).

### Production hardening

What a deployment owes that the platform does not provide: the identity
environment, cloud infrastructure and secret management, wiring metrics and
tracing to a collector, and the operational requirements in
[adoption.md](adoption.md).

Note that rate limiting, quotas, abuse controls and account lifecycle are **not
implemented** — for anything beyond an internal perimeter they are built, not
configured.

### Ongoing engineering

Continued development against a running deployment, including console screens
for a specific operational context (extension point 6).

---

These map onto the engagement progression in
[revenue-path.md](revenue-path.md#a-possible-engagement-progression), which is
equally clear that it has never been run.

**No prices appear anywhere in this repository, and none should be inferred.**

## Self-hosting

Self-hosting is the only way to run this today, and it is a first-class path
rather than a fallback: `docs/deployment.md` is a real runbook, the production
images are real, and the deployed Compose stack refuses to start on any secret
this repository publishes.

What self-hosting means in practice:

- Your host, your PostgreSQL, your backups, your monitoring.
- One outbound dependency — the model provider you configure. Anthropic and
  OpenAI both work; which one served a request is deliberately absent from
  every durable row.
- Your responsibility for everything in the
  [adoption checklist](adoption.md) that the platform does not do: rate
  limiting, retention, incident response, account lifecycle.

The MIT licence means you may do this commercially, for yourself or for a
client, with no obligation to the project.

## A hosted offering

**Possible future direction only. It does not exist and no work has started.**

It would be a significant undertaking rather than a deployment exercise: the
platform is multi-tenant at the data layer but has no billing, no quotas, no
rate limiting, no sign-up flow, no account lifecycle and no operational tooling
for running other people's workloads. Those are the features that turn a
self-hostable application into a service, and none of them is implemented.

Recorded here so the question has a documented answer, not to suggest a
roadmap.

## Contact

**The intended route for commercial and architecture enquiries is GitHub
Discussions** on this repository — the same place as questions about the
architecture, integrations and ideas.

> **Status: Discussions is not enabled yet.** It is a repository setting rather
> than a file, so it cannot be turned on by a commit — see
> [owner-actions.md](owner-actions.md#discussions). Until it is on,
> [GitHub Issues](https://github.com/BelgdevKing/ai-operations-agent/issues/new?template=question.md)
> is the only route that exists.

No email address, contact form or scheduling link is published in this
repository, and none has been invented for one. That is deliberate: an invented
address would be the one fabricated detail in a repository that otherwise states
plainly what does and does not exist.

Two things follow from routing enquiries through Discussions rather than a
private address:

- **The conversation starts in public.** For an architecture question that is an
  advantage — the answer is useful to the next reader. For anything involving a
  client's systems, data or security posture, move it out of public view early.
- **There is no response-time commitment.** This is a solo project developed in
  public and unfunded; see [Getting help](../README.md#getting-help).

## Attribution

The MIT licence requires the copyright notice and licence text to travel with
copies and substantial portions of the software. Beyond that there is no
attribution requirement, no logo usage policy and no trademark — because there
is no trademark.

---

**Related:** [evaluation.md](evaluation.md) for whether it holds up ·
[use-cases.md](use-cases.md) for whether it fits ·
[adoption.md](adoption.md) for what a deployment still owes ·
[extensions.md](extensions.md) for how to build on it.
