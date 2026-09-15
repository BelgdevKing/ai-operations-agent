# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting: go to the **Security** tab of
this repository and choose **Report a vulnerability**. That opens a private
advisory visible only to the maintainer, and it needs no email address from
either side.

> If the Security tab shows no reporting option, private reporting has not been
> enabled on the repository yet. Please open a public issue saying only that
> you have a security report and would like a private channel — no details —
> and one will be arranged.

There is no published contact address for this project and no bug-bounty
programme. This is an unfunded open-source project maintained by one person;
please set your expectations for response time accordingly. Reports will be
acknowledged and taken seriously, but there is no guaranteed turnaround.

### What helps

- What an attacker can achieve, not only what looks wrong.
- The version — a commit hash is ideal.
- Steps to reproduce, and whether it needs an authenticated account.
- Whether it crosses a tenant boundary. That is the property this project cares
  most about.

## Supported versions

There are no releases yet. The `main` branch is the only supported version, and
fixes land there.

## Scope

In scope, and most valuable:

- **Cross-tenant access.** Any path where one organization can read or change
  another's data. Tenant isolation rests on one repository base class plus
  composite foreign keys, and a hole in either is the most serious bug this
  project can have.
- **Authentication and authorization.** Token forgery, privilege escalation
  between the owner/admin/member roles, or acting as an organization you are
  not a member of.
- **The approval gate.** Anything that lets a destructive tool execute without
  an approval, execute twice, or execute after a rejection or an expiry.
- **Prompt injection with consequences.** Model output is treated as untrusted
  input: it names a tool and supplies arguments, both validated before anything
  runs. A path where model output reaches execution without that validation is
  in scope. A model simply saying something undesirable is not.
- **Secret disclosure.** A credential reaching a log, an error response, a
  health response, a metric, a span, a built image or the browser bundle.
- **Telemetry leakage.** A tenant identifier, an execution identifier, a prompt,
  a tool argument or a tool result appearing in metrics or traces.

Out of scope:

- Findings that require the development defaults. The shipped JWT secret and
  database password are published in this repository on purpose, and a deployed
  environment refuses to start on either.
- Anything needing host or database access you already have.
- Missing hardening that the documentation already states is absent — see
  *Known limitations* below.
- Automated scanner output with no demonstrated impact.
- Denial of service by sending a lot of traffic. There is no rate limiting, and
  that is known and documented rather than a finding.

## What the project already does

Stated so you can tell a real finding from intended behaviour:

- Passwords are Argon2id above the OWASP minimum. Tokens are JWT restricted to
  HMAC algorithms, so algorithm confusion is refused by configuration.
- The active organization arrives in a header that is verified against the
  caller's membership on every request. It selects a tenant; it never grants
  access to one.
- Secrets come from the environment. They are never copied into an image,
  echoed at start-up, or included in a health response. Configuration errors
  are rendered without the value that was rejected.
- Metrics and traces carry no tenant identifier, execution identifier, prompt,
  tool argument or result. Span attributes are an allow-list, and any
  UUID-shaped value is dropped whatever key it arrives under.
- The frontend stores nothing in the browser — no token in `localStorage`,
  `sessionStorage`, cookies or IndexedDB.
- Errors above 500 return a generic message and a correlation id; the detail
  stays in the server log.

## Known limitations

Not vulnerabilities — absent features, documented so nobody spends time
rediscovering them:

- No rate limiting and no per-tenant quotas.
- No token revocation. An access token is valid until it expires.
- No PostgreSQL row-level security. Isolation is enforced in the repository
  layer and by database constraints.
- No password reset and no user invitations.
- No security audit has been performed, and the project has no users.

## Deploying it safely

If you are running this somewhere real, [docs/deployment.md](docs/deployment.md)
is the runbook. The essentials: generate every secret, terminate TLS in front of
it, keep the database off the host's interfaces, and let the start-up checks do
their job rather than working around them.
