# Owner actions

Things that live in GitHub's settings rather than in this repository, and
therefore cannot be configured by a commit.

**Nothing on this page has been done.** It is a checklist with suggested values,
not a record of configuration. Each item is the project owner's decision; the
suggestions are starting points, not recommendations to follow blindly.

---

## Repository settings

### Description

The one line that appears under the repository name in search results and on the
profile. GitHub allows 350 characters; short is better.

> Self-hosted multi-tenant AI agents for operations — the model proposes tool
> calls, the platform authorizes, executes, and stops for human approval before
> anything destructive.

If that is too long for the space, the first clause alone carries the idea.

### Topics

Topics are how the repository is found by someone browsing rather than
searching. GitHub allows up to 20; fewer and more accurate is better than
twenty vague ones. Candidates, all accurate to what is implemented:

```
ai-agents  llm  tool-use  human-in-the-loop  approval-workflow
multi-tenant  fastapi  python  nextjs  typescript  postgresql
self-hosted  anthropic  openai  observability  agent-framework
```

Avoid topics that imply something untrue — `production-ready`, `enterprise`,
`rag` (retrieval is not implemented), `saas` (there is no hosted service).

### Social preview image

The card shown when the repository is linked from elsewhere. 1280×640.

No image exists in this repository and none was generated. A useful one would
show the thing that distinguishes the project rather than a logo: the
model-proposes/platform-disposes boundary, or a console screenshot at
`awaiting_approval`. See [Screenshots](#screenshots) below for why neither
exists yet.

### Visibility

The repository is not public yet. Everything in it has been written on the
assumption that it will be read by strangers — no secrets, no private paths, no
internal references — so making it public is a decision rather than a cleanup
task.

Worth doing before flipping it: read [SECURITY.md](../SECURITY.md) and confirm
the scope section says what you want it to say, since it becomes a public
commitment about how reports are handled.

### Discussions

Off by default. Worth enabling only if you intend to answer; an empty
Discussions tab reads worse than an absent one. Issues plus the templates in
`.github/ISSUE_TEMPLATE/` already cover bugs, proposals and
deployment/integration/architecture questions.

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

There is none. No email address, contact form or scheduling link appears
anywhere in the repository, and none has been invented. The README and
[commercial.md](commercial.md) both say so plainly rather than leaving a
placeholder.

Until one exists, GitHub Issues is the only route. If you want commercial
enquiries, a route has to be created and then referenced from
[the README](../README.md#if-this-is-useful-to-your-team),
[commercial.md](commercial.md#contact) and [SECURITY.md](../SECURITY.md).

### Whether to offer paid work at all

[commercial.md](commercial.md#potential-professional-services) lists ten kinds
of work the codebase could be a foundation for and states plainly that none is
offered, none has been performed and no rate has been set. Whether any becomes
real — and whether the project wants that — is unanswered.

A hosted offering is documented as a *possible future direction only*, with the
missing pieces named: billing, quotas, rate limiting, sign-up, account lifecycle
and operational tooling. None of it is started.

### Repository name and organization

`ai-operations-agent` under a personal account. Moving to an organization
changes every URL in the documentation; the links in this repository are
relative where possible, but `README.md`, `SECURITY.md` and
`.github/ISSUE_TEMPLATE/config.yml` each contain absolute GitHub URLs that would
need updating.

### Code of conduct

Deliberately absent. It is boilerplate until there are contributors for it to
govern, and adding it early is a signal of process rather than of care. Worth
adding when the first outside contributor arrives.

### Whether to push

`main` is many commits ahead of `origin/main`, which still sits at an early
commit — check with `git status -sb`. Publishing is a decision rather than a
chore: everything here has been written on the assumption a stranger will read
it, so pushing is the moment the project effectively becomes public, whatever
the visibility setting says.

Worth doing first: read [SECURITY.md](../SECURITY.md) and confirm its scope
section says what you are willing to commit to publicly.

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
