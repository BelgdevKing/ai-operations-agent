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
- **No paid tier.** The MIT licence covers everything in the repository; there
  is no second, commercial edition.
- **No support contract, SLA or warranty.** The licence's "as is" is the whole
  of it.
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

| Potentially | What it would involve |
| --- | --- |
| **Deployment assistance** | Standing the platform up in an environment, working through `docs/deployment.md` against real infrastructure |
| **Cloud integration** | Adapting the Compose deployment to a managed platform — container service, managed PostgreSQL, secret manager |
| **Internal-system integration** | Connecting it to the systems it needs to read and act on |
| **Custom tools** | Tools against those systems, with the safety classes and approval policy that match the risk |
| **Workflow development** | Operational procedures built on the existing execution and approval architecture |
| **Model and provider integration** | A different provider, or a self-hosted model, behind the existing gateway |
| **Architecture review** | Tenant isolation, approval semantics, agent safety, observability and reliability — for a team building something comparable |
| **Security and tenant-boundary review** | Specifically the isolation and approval boundaries, and what a deployment adds around them |
| **Console work** | Screens for a specific operational context |
| **Ongoing engineering support** | Continued development against a particular deployment |

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

> **Commercial contact route: not configured yet.**
>
> No email address, contact form or scheduling link is published in this
> repository, and none has been invented for one. GitHub Issues is the only
> route that exists today.

Establishing a contact route is an open owner decision — see
[owner-actions.md](owner-actions.md#commercial-contact-route). Until then,
nothing on this page can be acted on beyond using the software, which the
licence already permits.

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
