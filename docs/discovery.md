# Discovery

What to establish in a first conversation about building on this architecture,
and what a first piece of work could concretely be.

> **Nothing here has been used with a client.** No engagement has been run, no
> discovery has been conducted, no proof of concept has been delivered, and no
> price appears anywhere. This is a prepared framework, not a record of practice.

Written to be read from both sides. If you are evaluating whether this
architecture fits your organization, knowing what you would be asked — and what
would deliberately *not* be promised — is a reasonable thing to want before
starting a conversation.

The self-service half comes first and involves nobody:
[evaluation.md](evaluation.md) to check the claims, [demo.md](demo.md) to watch
the approval boundary work. A conversation is only worth having after those.

---

## Who this tends to fit

Fit criteria, not a ranking. Each profile names what the repository already
provides and what would have to be built, because the second list is the work.
[client-problems.md](client-problems.md) covers the same ground organised by
problem rather than by organization.

### A team whose AI prototype works but is not allowed to act

**The situation.** The assistant answers well. Nobody will connect it to a
system that can change anything, because the failure mode is a real customer's
real record and nobody can say precisely what prevents it.

**The technical signal.** Model output is passed to a function that executes
directly. There is no point where a check could be inserted, because proposing
and executing are the same step.

**Already provided.** The proposal/execution split, eight validation gates, and
a safety class a `destructive` tool cannot opt out of —
[`backend/app/tools/executor.py`](../backend/app/tools/executor.py).

**Would be customized.** Every tool against their systems. Which of their
actions count as destructive — their judgement, and the question that shapes
everything else.

**Plausible first step.** Architecture review, then a narrow proof of concept.

**Not promised.** That an existing prototype ports cheaply. Moving functions
behind the tool interface is a real port —
[extensions.md](extensions.md#1-a-read-only-tool) shows the size of it.

### A team that needs a human decision before high-impact actions

**The situation.** Everyone agrees there should be a person in the loop. Nobody
has built it, because doing it properly means a durable pause, a queue, a
decision that survives a restart, and an action that runs exactly once.

**The technical signal.** Approval exists as a UI confirmation, or as a flag in
memory, or not at all.

**Already provided.** All four, as the default path: `awaiting_approval` is a
database row, the decision is a conditional `UPDATE` so two approvers produce
one decision and one conflict, execution is claimed the same way, and an
unanswered approval expires rather than counting as a refusal.

**Would be customized.** Who may approve what. The allow-listed summary an
approver sees for each action. **Notification — there is none.** The inbox is a
screen someone opens.

**Plausible first step.** A proof of concept covering one real destructive
action.

**Not promised.** That approvals reach anyone. No email, no Slack, no push; that
integration is work.

### An organization that must account for what an AI did

**The situation.** Somebody will ask why a record was changed, and "the
assistant did it" does not survive the question.

**The technical signal.** Actions are logged as free text, or logged by the same
code that performs them, or not correlated to a decision-maker.

**Already provided.** Every run, step, tool execution, approval and decision as
a durable row — what was proposed, who decided, which way, when, and whether the
action then ran. Audit events for the significant transitions.

**Would be customized.** Retention and deletion policy. Reporting.

**Not promised.** Compliance. There is **no audit API and no audit screen** —
events are written and nothing reads them back over HTTP. If a regulator or a
customer is owed a report, that layer is built, not configured. No certification
of any kind exists.

### A team running one AI system for several internal groups

**The situation.** A prototype that assumed one group now has a second, and the
failure mode is one group seeing another's data.

**The technical signal.** Tenant filtering written per query, or enforced only
in application code, or absent.

**Already provided.** `organization_id` on every tenant-owned row, one base
class where the filter is written, and composite foreign keys that make a
cross-tenant reference unrepresentable rather than merely rejected.

**Would be customized.** Their entities. What a tenant owns.

**Not promised.** Row-level security — **not implemented**. A query bypassing
the repository layer is caught only by database constraints, and only for
cross-tenant references. [evaluation.md](evaluation.md#how-tenant-isolation-works)
sets out exactly how far it goes.

### A team that cannot send operational data to a third party

**The situation.** The data that would make an assistant useful is the data that
cannot leave.

**Already provided.** MIT licensed, self-hostable in full. No key, no account,
no hosted dependency, no phone-home. One outbound call, to the provider
configured; Anthropic and OpenAI sit behind one gateway and a self-hosted model
is an adapter away.

**Would be customized.** Their infrastructure, identity provider, secret
management and monitoring.

**Not promised.** That self-hosting is free of operational cost.
[adoption.md](adoption.md) is deliberately blunt about what a deployment owes.

### Where this does not fit

Worth reaching quickly.
[client-problems.md](client-problems.md#where-this-is-the-wrong-tool) lists four
situations where the honest answer is no — open-ended chat over documents, since
retrieval is not built; high-volume unattended action, since the design assumes
a person in the loop; sub-second latency; and a public-facing product, since
there is no rate limiting, no abuse control and no account lifecycle.

A discovery that concludes "this is the wrong architecture for you" is a
successful discovery.

---

## What to establish

Fifteen areas. The point is to find out whether the architecture fits, which
includes finding out that it does not. None of these assumes an answer.

### The system as it stands

1. **Current AI architecture.** What exists now — a prototype, a shipped
   feature, nothing yet? Where does model output go, and what can it reach?
2. **The workflow to be automated.** One concrete operational procedure, start
   to finish, as a person performs it today. Not a category of work.
3. **Providers and models.** Which provider, which models, whether that is a
   settled decision, and whether a self-hosted model is required.

### What the agent would touch

4. **Internal APIs and tools.** What exists, how it authenticates, its rate
   limits and its failure behaviour. All four determine how tools are written.
5. **Data sources.** Which systems of record, their shape, and whether they can
   be reached from wherever the platform would run.
6. **Authentication and authorization.** The existing identity provider, whether
   SSO or a directory is mandatory, and who is allowed to do what.

### The boundaries

7. **Approval requirements.** Which actions need a human decision, who may make
   it, and what that person must see in order to decide.
8. **Tenant isolation requirements.** Whether data must be separated between
   groups, how strictly, and whether that separation has to be demonstrable.
9. **Audit requirements.** What must be recorded, who reads it back, in what
   form, and how long it must be kept.

### The environment

10. **Deployment environment.** Where it would run, who operates it, and what it
    must integrate with — secrets, monitoring, backups.
11. **Security and compliance requirements.** Threat model, network policy, key
    management, residency, right to erasure. The repository documents its own
    boundaries in [evaluation.md](evaluation.md); it cannot assume theirs.

### The gap and the goal

12. **Current prototype limitations.** What specifically is blocking it —
    safety, isolation, auditability, reliability, or something else entirely.
13. **Desired proof of concept.** Which single workflow would be most convincing
    to the people who have to be convinced.
14. **Success criteria.** What would have to be observably true for the proof of
    concept to have answered the question. Agreed before it starts, not after.
15. **What happens after.** Whether there is a path to production, who would own
    it, and what would have to be true for that to be approved.

Two questions worth asking last, because the answers are often more useful than
anything above: **what would make you decide against this?** and **who else has
to agree?**

---

## What a proof of concept could be

An illustration of scope, not an offer. No such engagement has been performed.

It is deliberately narrow because the goal is to answer one question — does this
approach survive contact with their environment — and a narrow build answers it
sooner than a broad one.

### A plausible shape

| | |
| --- | --- |
| **One workflow** | A single real operational procedure, chosen during discovery |
| **Two tools** | One read-only against a real system of record, one destructive against a real record |
| **The safety boundary** | The destructive tool declared `destructive`, so approval is enforced by the metadata rather than by remembering to check |
| **Human approval** | The gate working end to end, with the allow-listed summary an approver actually needs |
| **Audit trail** | The run, steps, tool executions, approval and decision as durable rows |
| **Evaluation** | The success criteria agreed in discovery, checked against what the system does |

In effect it is the demonstration in [demo.md](demo.md) with their data in place
of the freight dataset, their action in place of a shipment cancellation, and
their approver deciding.

### Three things held apart

Kept distinct on purpose, because conflating them is how expectations drift:

- **Repository functionality.** Exists today, is covered by tests, and can be
  verified before anyone is engaged — which is the entire point of
  [evaluation.md](evaluation.md).
- **Customization.** Does not exist and would be built: the tools, the data
  access, the approval policy, the console changes.
- **Professional services.** The work of doing the second thing. Currently
  **none is offered or sold** — [commercial.md](commercial.md) is explicit
  about that.

### Explicitly outside a proof of concept

Saying so early is more useful than discovering it late. None of these is a
configuration step:

- Production readiness. A proof of concept demonstrates an approach; it is not a
  deployment.
- Rate limiting, quotas and abuse controls — **not implemented**.
- Token revocation, user invitations, password reset — **not implemented**.
- An audit API or reporting layer — events are recorded, nothing reads them back.
- Retrieval over documents — **not built**.
- Notifying approvers.
- Security certification, penetration testing or load testing.

### If it answers the question

The follow-on work is conditional, and each step should be able to end the
engagement: the remaining tools and workflows, the identity environment,
production hardening against [adoption.md](adoption.md), observability wired to
a real collector, and continued engineering against a running deployment.
[revenue-path.md](revenue-path.md#a-possible-engagement-progression) sets out
that progression in full and is equally clear that it has never been run.

---

## Starting a conversation

The intended route is **GitHub Discussions** on this repository.

[Open a discussion](https://github.com/BelgdevKing/ai-operations-agent/discussions).

No email address, contact form or scheduling link is published in this
repository, and none has been invented. A conversation that touches a client's
systems, data or security posture should move out of public view early.

---

**Related:** [evaluation.md](evaluation.md) — verify the claims first ·
[client-problems.md](client-problems.md) — the same ground by problem ·
[service-brief.md](service-brief.md) — the scope-level view ·
[commercial.md](commercial.md) — what is and is not offered ·
[revenue-path.md](revenue-path.md) — how far along that path this is.
