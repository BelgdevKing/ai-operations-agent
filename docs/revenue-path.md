# Revenue path

How an open-source project like this could lead to paid engineering work, and
exactly how far along that path it currently is.

> **Current position: step zero.** The repository is private, unpublished, and
> has no users, no enquiries, no engagements, no revenue and no contact route.
> Everything below "what exists today" is a plan, not a pipeline.

This document exists so the question has an honest written answer — for the
owner, and for anyone evaluating whether help could ever be available. It is not
a business plan and it does not describe a business.

---

## What exists today

Verifiable from the repository. [evaluation.md](evaluation.md) says how to check
each one.

| | |
| --- | --- |
| **An open-source project** | MIT licensed, self-hostable in full, no key or account required |
| **Agent runtime** | A tool-use loop with a bounded step budget; runs are durable rows |
| **Tool execution** | A typed registry with declared safety classes, schema-validated arguments, per-call timeouts, bounded results |
| **Human approval boundary** | Destructive tools cannot execute unattended; at-most-once execution settled in the database |
| **Workflows** | Multi-step definitions as a durable state machine, with approval gates at any step |
| **Multi-tenancy** | `organization_id` throughout, one filter, composite foreign keys |
| **Usage and cost tracking** | Per organization, derived at read time, with a configurable price book |
| **Observability** | Prometheus metrics and W3C traces, both off by default, neither carrying tenant or execution identifiers |
| **Reliability controls** | Every external wait bounded; dependency outages as `503`; only the gateway retries |
| **Console** | Agent console, approval inbox, workflows, usage, members |
| **Deployment infrastructure** | Production images, a deployed Compose stack, migrations as their own job, a runbook |
| **1,781 backend and 293 frontend tests** | Written against boundaries rather than for coverage |

**What does not exist:** customers, revenue, engagements, enquiries, a hosted
service, a paid tier, a support contract, published rates, a contact route, or
any production deployment. [commercial.md](commercial.md#what-does-not-exist)
lists the absences in full.

## Why an open-source project can lead to paid work

The mechanism, stated plainly rather than assumed:

A team with the problems in [client-problems.md](client-problems.md) finds a
repository that already solves the architectural half — the approval boundary,
the tenancy, the durable execution, the audit trail — and can verify that in an
afternoon rather than take it on trust. What they still need is the half that
cannot be open-sourced: **their systems, their data model, their security
environment, their deployment.**

That gap is the work. The repository's job is to make the architectural half
credible and inspectable, so the conversation starts from "connect this to our
systems" rather than "convince me this approach is sound."

Which is why the effort so far has gone into [evaluation.md](evaluation.md),
[demo.md](demo.md) and the test suite rather than into marketing: an evaluator
who can check the claims needs less persuading than one who is asked to believe
them.

## What could be offered professionally

**None of this is currently offered or sold.** No engagement has been performed,
no rate has been set, no client exists, and there is no contact route through
which any of it could be requested. Listed unranked — not by profitability, not
by preference.

| Potential work | Rough shape |
| --- | --- |
| **AI agent implementation** | Building the agents and the loop around a specific operational domain |
| **Internal-system integration** | Connecting the platform to the systems it must read and act on |
| **Custom tool development** | Typed tools, with the safety classes and approval policy that match the risk |
| **Workflow automation** | Operational procedures on the existing execution and approval architecture |
| **LLM and provider integration** | A different provider, or a self-hosted model, behind the existing gateway |
| **AI architecture consulting** | Agent safety, the proposal/execution split, approval design — for teams building their own |
| **Deployment assistance** | Standing it up in a real environment, against real infrastructure |
| **Security and tenant-boundary review** | The isolation and approval boundaries, and what a deployment must add around them |
| **Custom console development** | Screens for a specific operational context |
| **Existing-system modernization** | Replacing manual operational procedures with agent workflows under approval |

[service-brief.md](service-brief.md) has the scope-level version of this, and is
equally explicit that none of it is currently sold.

## A possible engagement progression

**Proposed, not established.** No client has been through this; it has never
been run. It is written down because "what would working together look like" is
a reasonable question to have an answer to, and inventing one on the spot is
worse than having thought about it.

```
Discovery
    │  What the systems are, which actions matter, which are destructive.
    │  Output: whether this architecture fits at all — including "it does not".
    ▼
Architecture review
    │  The proposal/execution split, tenant boundaries, approval policy,
    │  what the deployment environment adds.
    │  Output: a written assessment. Useful even if nothing else follows.
    ▼
Small proof of concept
    │  One real read-only tool and one real destructive tool, against a real
    │  system, with the approval gate working.
    │  Output: the thing from demo.md, with their data instead of shipments.
    ▼
Integration
    │  The rest of the tools, the workflows, the identity environment,
    │  the persistence and the console changes.
    ▼
Production hardening
    │  What adoption.md says a deployment owes: secrets, backups, monitoring,
    │  rate limiting, retention, incident response, account lifecycle.
    ▼
Ongoing engineering
       Continued development against a running deployment.
```

Two honest notes about it:

- **Each step should be able to end the engagement.** Discovery concluding "this
  is the wrong architecture for you" is a successful discovery.
- **Production hardening is not a formality.** The platform has no rate
  limiting, no abuse controls and no account lifecycle; a deployment facing
  anything beyond an internal perimeter needs those built.

## What would have to be true first

The gap between this document and any revenue, listed as prerequisites rather
than as a roadmap. All are owner decisions —
[owner-actions.md](owner-actions.md#revenue-activation):

1. **The repository is published.** Nothing can be found while it is private.
2. **A contact route exists.** There is none, and none has been invented. Today
   the only route is GitHub Issues.
3. **A decision to offer paid work at all.** Unanswered.
4. **Somebody finds it and has one of the problems.** Not something the
   repository can arrange.

Steps 1 and 2 are small and entirely within the owner's control. Step 4 is not,
and no amount of documentation makes it certain.

## What this document is not

It is not a forecast, a plan of record, or evidence of demand. Nothing in it
should be read as a claim that work is available, that anyone has asked, or that
the project earns anything. If that changes, it will be stated here as fact
rather than implied.

---

**Related:** [commercial.md](commercial.md) — what the licence permits and what
does not exist · [service-brief.md](service-brief.md) — the scope-level view ·
[client-problems.md](client-problems.md) — the problems this addresses ·
[owner-actions.md](owner-actions.md) — the decisions still open.
