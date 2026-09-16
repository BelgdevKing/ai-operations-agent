# Owner actions

Things that live in GitHub's settings rather than in this repository, and
therefore cannot be configured by a commit.

Each item is the project owner's decision; the suggestions are starting points,
not recommendations to follow blindly. Anything marked **Done** was verified
against GitHub's public API rather than assumed — everything else is still
open, and nothing here claims a setting was changed by a commit.

**Two are done: the repository is public and pushed.** The single item that now
gates everything commercial is [Discussions](#discussions), which is off.

---

## Repository settings

### Description

**Currently unset — verified.** This is the first thing a stranger reads, and on
a public repository with no description GitHub shows nothing at all where the
one-line pitch should be. Of everything on this page it is the cheapest with the
most direct effect on being found.

The one line that appears under the repository name in search results and on the
profile. GitHub allows 350 characters; short is better.

> Open-source AI operations agent platform with safety boundaries, approvals,
> workflows, observability, and extensible business tools.

Every claim in it is implemented: the safety boundary is
[`backend/app/tools/executor.py`](../backend/app/tools/executor.py), approvals
and workflows are durable state machines, observability is Prometheus metrics
and W3C traces, and the five shipped tools are the extensible part.

An earlier alternative, kept because it leads with the differentiator rather
than the feature list:

> Self-hosted multi-tenant AI agents for operations — the model proposes tool
> calls, the platform authorizes, executes, and stops for human approval before
> anything destructive.

### Topics

**Currently none — verified.** Topics are how the repository is found by someone
browsing rather than searching, and with none set it appears in no topic
listing. GitHub allows up to 20; fewer and more accurate is better than twenty
vague ones. Candidates, all accurate to what is implemented:

```
ai  ai-agents  agentic-ai  llm  generative-ai
python  fastapi  react  postgresql
workflow-automation  human-in-the-loop  approval-workflow
tool-use  multi-tenant  self-hosted  observability
```

Each is accurate to what is implemented. `react` covers the Next.js 15 / React
19 console; `workflow-automation` covers the durable workflow engine;
`agentic-ai` and `tool-use` cover the agent loop and the typed tool registry.

**Deliberately excluded: `rag`.** Retrieval is not implemented — there is a
`documents` table holding metadata and an empty `app/knowledge/` package, and
nothing else: no upload, extraction, chunking, embedding or vector search. A
`rag` topic would put the repository in front of people looking for exactly the
capability it does not have, which wastes their time and costs the project the
credibility the rest of this documentation is built on. Add it if and when
retrieval is built.

Also avoid `production-ready`, `enterprise` and `saas` — none is true today.

### Social preview image

The card shown when the repository is linked from elsewhere. 1280×640.

No image exists in this repository and none was generated. A useful one would
show the thing that distinguishes the project rather than a logo: the
model-proposes/platform-disposes boundary, or a console screenshot at
`awaiting_approval`. See [Screenshots](#screenshots) below for why neither
exists yet.

### Visibility

**Done — the repository is public.** Verified against GitHub's public API
rather than assumed.

What was checked before publication, recorded as a point-in-time audit rather
than a standing guarantee:

- **All 361 tracked files** scanned for provider and cloud credential prefixes,
  PEM private-key markers, JWT-shaped tokens, credentialed database URLs and
  literal secret assignments. Every match was inspected. All are either the
  development defaults this repository publishes on purpose — and which a
  deployed environment refuses to start on — or test fixtures whose whole
  purpose is to assert that a value never escapes.
- **All 592 blobs across the 23 commits of history**, for the same credential
  patterns. Nothing found.
- **No `.env`, key, log, dump, build output or cache has ever been committed**,
  on any branch, at any point in the history.

An automated scan finds credential-shaped strings, not a comment that names an
internal system or an example that happens to be a real customer. The repository
was also read through for those, and nothing of the kind was found. No email
address in it is a contact address: they are demo-dataset and test-fixture
values on reserved domains — `example.com`, `.example`, `.test`, `.internal` —
apart from one deliberately malformed string in an input-validation test.

One consequence of publication worth knowing: **every commit carries the
committer's email address**, which is now public along with the code. GitHub
offers a `noreply` address for anyone who would rather it were not — changing it
affects future commits, not the 23 already pushed.

### Discussions

**Off. Verified against GitHub's API, not assumed — and this is now the item
that gates the contact route.**

[commercial.md](commercial.md#contact) names Discussions as the intended route
for commercial and architecture enquiries, and says plainly that it is not
enabled yet. Turning it on is **Settings → General → Features → Discussions**.

The earlier reasoning here — that an empty Discussions tab reads worse than an
absent one — still applies to a repository with contributors and no answers. It
applies less to this one, where Discussions would be the only enquiry route that
is not a bug tracker, and where an empty tab is the normal state of a project
with no users yet.

Suggested categories, matching what the documentation already routes there:

| Category | For |
| --- | --- |
| **Q&A** | Deploying it, integrating it, why a boundary works the way it does |
| **Architecture** | The proposal/execution split, tenant isolation, approval semantics |
| **Integrations** | Connecting it to real systems; tools against a specific domain |
| **Ideas** | Proposals that are not yet a concrete feature request |
| **Show and tell** | What someone built on it |

Issues and the templates in `.github/ISSUE_TEMPLATE/` already cover bugs,
proposals and questions, and should stay as they are — Discussions is for the
conversations that do not belong in a tracker.

**After enabling, one small follow-up:** add a Discussions link to
`.github/ISSUE_TEMPLATE/config.yml`, which deliberately does not contain one
today because the link would 404 while the feature is off.

### Private vulnerability reporting

**Settings → Security → Private vulnerability reporting.**

[SECURITY.md](../SECURITY.md) routes reports here and documents a fallback for
the case where it is not enabled — but the fallback is worse for everyone, since
it asks a reporter to open a public issue saying they have something private.
This is the cheapest item on the page and the one with the clearest benefit.

### Funding

`.github/FUNDING.yml` has deliberately **not** been created. It requires a real
funding destination — a GitHub Sponsors profile, or an account elsewhere — and
inventing a URL for one would be exactly the kind of fabricated detail the rest
of the repository avoids. Create the file if and when a destination exists.

---

## Decisions that are not GitHub settings

### Commercial contact route

**Decided: GitHub Discussions. Not yet active, because Discussions is off.**

No email address, contact form or scheduling link appears anywhere in the
repository, and none has been invented. That was a deliberate choice rather than
an oversight — see [Discussions](#discussions) for the one action that makes the
chosen route real.

Discussions was preferred over publishing an address for three reasons worth
recording: it exposes no personal data on a public repository, it costs nothing
and is reversible, and an architecture answer given in public is useful to the
next reader. Its limitation is equally real — a conversation about a client's
systems, data or security posture should move out of public view early, and
[commercial.md](commercial.md#contact) says so.

If an address or scheduling link is ever added instead, it belongs in exactly
three places: [the README](../README.md#if-this-is-useful-to-your-team),
[commercial.md](commercial.md#contact) and [SECURITY.md](../SECURITY.md).

### Whether to offer paid work at all

[commercial.md](commercial.md#potential-professional-services) lists five
categories of work the codebase could be a foundation for — architecture review,
proof of concept, custom integration, production hardening, ongoing engineering
— and states plainly that none is offered, none has been performed and no rate
has been set. Whether any becomes real, and whether the project wants that, is
unanswered.

A hosted offering is documented as a *possible future direction only*, with the
missing pieces named: billing, quotas, rate limiting, sign-up, account lifecycle
and operational tooling. None of it is started.

### Repository name and organization

`ai-operations-agent` under a personal account. Moving to an organization
changes every URL in the documentation; the links in this repository are
relative where possible, but `README.md`, `SECURITY.md` and
`.github/ISSUE_TEMPLATE/config.yml` each contain absolute GitHub URLs that would
need updating.

### Revenue activation

[revenue-path.md](revenue-path.md#what-would-have-to-be-true-first) sets out why
the first two gate everything else.

1. ~~**Publish the repository.**~~ **Done.** It is public; see
   [Visibility](#visibility).
2. **Enable Discussions.** The chosen contact route, and the only item here that
   costs nothing, takes one click and is reversible. Until it is on, nothing in
   [service-brief.md](service-brief.md) can be acted on — see
   [Discussions](#discussions).
3. **Set the description and topics.** Publication makes the repository
   reachable; these make it findable. Both are still unset.
4. **Decide whether to offer paid implementation work at all.** Unanswered, and
   a legitimate answer is no.
5. **Decide whether to publish a service profile** — on GitHub, a personal site,
   or not at all.
6. **Decide whether to create a GitHub Sponsors destination.** Required before
   `.github/FUNDING.yml` can exist; see [Funding](#funding).
7. **Decide whether to create a portfolio or case-study page.** Note that there
   are no cases to study — no engagement has been performed.
8. **Decide whether to record a public demo video.** See
   [Screenshots](#screenshots) for why none exists yet.
9. **Decide whether to add a scheduling mechanism.** Only meaningful after 2
   and 4.

Items 5 to 9 all involve publishing a destination or an identity. **None has
been created or invented here**, and each would need the owner to supply the
real value. Items 2 and 3 need no new identity at all — they are settings on a
repository that already exists.

### Code of conduct

Deliberately absent. It is boilerplate until there are contributors for it to
govern, and adding it early is a signal of process rather than of care. Worth
adding when the first outside contributor arrives.

### Whether to push

**Done.** `main` and `origin/main` are in step, and CI runs on each push.

Kept as a note rather than deleted, because the reasoning still applies to every
future push: everything here is written on the assumption a stranger will read
it, and pushing is the moment that becomes true. [SECURITY.md](../SECURITY.md)
is a public commitment about how reports are handled — worth re-reading whenever
its scope section would change.

---

## Screenshots

None exist, and none were generated.

A browser binary is present on the development machine, but the screenshot worth
having — the Agent Console at `awaiting_approval`, which is the entire
differentiator in one image — cannot be captured from it:

- Every meaningful screen is behind `RequireAuth`, and the access token is held
  in memory only. Reaching one means filling in a sign-in form, which needs
  interactive browser scripting rather than a single-URL headless capture.
- Getting a run *into* `awaiting_approval` needs a model provider credential,
  which is not configured in this environment.

What could be captured without either — the landing page and the sign-in form —
demonstrates nothing about what makes the project interesting, so no screenshot
was added rather than adding a decorative one.

**To produce it yourself:** follow [demo.md](demo.md) with a provider key to
step 5, then screenshot the console showing the run paused and the approval
panel. A second useful frame is the usage summary on `/dashboard` after
deciding. Check any screenshot for the account email and organization names
before committing it.

A demo GIF or video is deferred for the same reason, and because recording one
would mean adding tooling the project does not otherwise need.
