# Revenue path

How an open-source project like this could lead to paid engineering work, and
exactly how far along that path it currently is.

> **Current position: step one of four.** The repository is public. It has no
> users, no enquiries, no engagements and no revenue, and its contact route —
> GitHub Discussions — is not enabled yet. Everything below "what exists today"
> is a plan, not a pipeline.

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

## The path from discovery to a conversation

The second half of this — everything after contact — is the progression further
down. This is the half the repository itself has to carry, because nobody is
being sold to at any point in it.

```
Finds the repository on GitHub
    |  Description, topics and social preview are what make this possible.
    |  All three are still unset - owner-actions.md.
    v
Reads the README
    |  First screen: what it is, the one distinction that matters,
    |  and how to run it.
    v
Understands what it is
    |  technical-overview.md - the whole architecture in one read.
    v
Runs it
    |  demo.md - about fifteen minutes to a run stopped at
    |  awaiting_approval, with the shipment untouched.
    v
Checks the claims rather than believing them
    |  evaluation.md - what is implemented, what is not, and what to
    |  test first. This is the step that earns the rest.
    v
Learns where it stops
    |  No retrieval, no audit API, no rate limiting, no token revocation.
    |  adoption.md - what a deployment still owes.
    v
Recognises their own problem in it
    |  client-problems.md - organised by problem, not by feature.
    v
Starts a conversation
       GitHub Discussions, once enabled. service-brief.md is what
       that conversation would be about.
```

Two things worth noticing about this path.

**Every step before the last is self-service, and most of them can end it.** A
reader who gets to `evaluation.md` and decides the absent row-level security is
disqualifying has been served well by the documentation, not failed by it. The
same is true of the four situations in
[client-problems.md](client-problems.md#where-this-is-the-wrong-tool) where this
is the wrong tool.

**Nothing in the path is a funnel.** There is no gate, no email capture, no
"book a call", no trial to convert. The repository is MIT licensed and complete;
somebody can run it forever without a conversation, and that is a legitimate
outcome rather than a leak.

## What could be offered professionally

**None of this is currently offered or sold.** No engagement has been performed,
no rate has been set, no client exists, and the contact route is not enabled
yet. Listed unranked — not by profitability, not by preference.

Five categories, defined once in
[commercial.md](commercial.md#potential-professional-services) and summarised
here:

| Potential work | Rough shape |
| --- | --- |
| **Architecture review** | The proposal/execution split, tenant boundaries, approval design — for teams building their own |
| **Proof of concept** | One real read-only tool and one real destructive tool against a real system, with the gate working |
| **Custom integration** | The systems the platform must read and act on; typed tools, workflows, a different provider |
| **Production hardening** | Identity environment, cloud infrastructure, observability wiring, and what a deployment still owes |
| **Ongoing engineering** | Continued development against a running deployment, including console work |

They correspond to the progression below, from architecture review onward —
everything after discovery.
[service-brief.md](service-brief.md) has the scope-level version, and is equally
explicit that none of it is currently sold.

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

[discovery.md](discovery.md) is the first two steps in working detail: the
profiles this tends to fit, what discovery would establish, and a scoped proof
of concept with the things it explicitly excludes.

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

1. ~~**The repository is published.**~~ **Done.** It is public, and the
   description, topics and social preview that make it findable are still unset
   — see [owner-actions.md](owner-actions.md#repository-settings).
2. **A contact route exists.** Not yet. The intended route is GitHub
   Discussions, which is a settings toggle away and costs nothing; until it is
   on, GitHub Issues is all there is.
3. **A decision to offer paid work at all.** Unanswered, and a legitimate answer
   is no.
4. **Somebody finds it and has one of the problems.** Not something the
   repository can arrange.

Step 1 is done. Step 2 is one toggle. Step 4 is neither, and no amount of
documentation makes it certain — publication makes the project findable, not
found.

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
