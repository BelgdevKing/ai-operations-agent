# Adoption checklist

What to check before evaluating this, before deploying it internally, and before
letting anyone outside your organization near it.

The platform does a specific set of things well and deliberately does not do
others. This page is the honest division of labour, so nothing important falls
between the software and the people running it.

Every item is one of four things:

| | |
| --- | --- |
| ✅ **Implemented** | The platform does this; verify it rather than build it |
| ⛔ **Intentionally absent** | Not built, and documented as not built |
| 👤 **Yours** | The deployer's responsibility, in any deployment |
| 🔭 **Future** | A plausible extension; no work has started |

---

## Before evaluating

Cheap checks that decide whether to keep going. [evaluation.md](evaluation.md)
has the commands.

| | Item | What to look at |
| --- | --- | --- |
| ✅ | **Architecture review** | Whether a modular monolith with no queue suits you. [architecture.md](architecture.md) |
| ✅ | **The trust boundary** | Whether "the model proposes, the platform disposes" holds in the code. [`app/tools/executor.py`](../backend/app/tools/executor.py) is the whole of it |
| ✅ | **Authentication and authorization** | Argon2id, JWT restricted to HMAC, owner/admin/member roles. Read `app/core/security.py` and `app/api/deps.py` |
| ✅ | **Tenant isolation** | One filter in `TenantScopedRepository`, composite foreign keys in the database. Run `pytest tests/integration/test_tenant_isolation.py` |
| ✅ | **Approval semantics** | Destructive tools cannot opt out; decisions settled by conditional `UPDATE`. Run `pytest tests/integration/test_approval_workflow.py` |
| ✅ | **Provider configuration** | Anthropic or OpenAI behind one gateway; a self-hosted model is an adapter away. [extensions.md](extensions.md#5-another-model-provider) |
| ✅ | **Deployment configuration** | Production images, a deployed Compose stack, migrations as their own job. [deployment.md](deployment.md) |
| ⛔ | **Retrieval (RAG)** | Not built. If your use case needs it, that is the gap to weigh first |

**The question worth asking here:** does your problem need an agent that *acts*,
or one that *answers*? This platform's architecture only pays for itself if
actions are involved — the approval gate, the run records and the safety classes
are all machinery for actions. For pure question-answering it is more structure
than the problem needs.

## Before deploying internally

| | Item | Position |
| --- | --- | --- |
| 👤 | **Secret management** | Secrets come from the environment; nothing is baked into an image or echoed at start-up, and a deployed environment refuses to start on any published default. **Where they come from is yours** — the runbook assumes an env file, not a secret manager |
| 👤 | **Database backups** | Nothing takes one. `pg_dump`/`pg_restore` commands are in [deployment.md](deployment.md#backups); scheduling, storing off-host and rehearsing a restore are yours |
| ✅ | **Logging** | Structured JSON, one line per request, correlation id on every line, nothing sensitive. **Shipping it somewhere is yours** |
| ✅ | **Monitoring** | Prometheus metrics behind a bearer token, off by default. One worker per container, because the registry is per process — see [deployment.md](deployment.md#workers-and-metrics). **Scraping, dashboards and alerts are yours** |
| ✅ | **Tracing** | Spans at six boundaries, W3C context, off by default, records to the log. **A collector is yours** — there is no OTLP exporter |
| ⛔ | **Rate limiting** | Not implemented. Nothing bounds how often a tenant calls a tool or how much it spends. Decide whether your deployment needs a limit in front of it |
| 👤 | **Token and session policy** | JWTs expire and there is ⛔ **no revocation list** — expiry is what ends a session. Set `ACCESS_TOKEN_EXPIRE_MINUTES` accordingly |
| ⛔ | **RAG decision** | Not built. Decide whether you need it before committing, not after |
| 👤 | **Operational ownership** | Who runs the migration, who holds the provider key, who is an admin and can therefore approve destructive actions |
| 👤 | **Maintenance jobs** | Two scripts expire stale approvals and sweep abandoned runs. They ship in the image; **scheduling them is yours** — [deployment.md](deployment.md#maintenance-jobs) |
| ✅ | **Graceful shutdown** | Bounded drain, pools closed, runs left recoverable. Nothing to configure beyond the grace periods already set |

**The decision most likely to be got wrong:** who is an admin. Approving is an
admin action, and registration makes you an owner of your own organization. In a
real deployment, deciding who can allow a destructive action is a policy
decision, not a default.

## Before exposing it externally

Everything here is either yours or absent. The platform is built to be
self-hosted behind your own perimeter; none of this is done for you.

| | Item | Position |
| --- | --- | --- |
| 👤 | **Threat model** | Start from [SECURITY.md](../SECURITY.md), which names what the project considers in scope. Your exposure is broader than the repository's |
| ⛔ | **Abuse controls** | None. No CAPTCHA, no registration throttle, no anomaly detection. Registration is open to anyone who can reach the endpoint |
| ⛔ | **Rate limiting** | Still none, and it matters far more here: an external caller can start agent runs, and runs cost money at your provider |
| ⛔ | **Account lifecycle** | ⛔ no invitations, ⛔ no password reset, ⛔ no email verification, ⛔ no deactivation flow beyond the member endpoints. 👤 Yours to build or to front with an identity provider |
| ⚠️ | **Audit requirements** | Audit events **are** written to the database. ⛔ Nothing reads them back over HTTP — no audit API, no audit screen, no export. If you owe someone an audit trail, you own the reporting |
| 👤 | **Data retention** | Conversations, runs, tool executions and approvals accumulate. Nothing expires them. Retention and deletion are yours |
| 👤 | **Privacy** | Conversation content and business records are in your database. What the model provider sees is what your tools return and your users type — your data-processing assessment, not the project's |
| 👤 | **Incident response** | Correlation ids tie a report to a log line, and traces show where a request went. The process around them is yours |
| 👤 | **TLS and the perimeter** | The containers publish to loopback; a reverse proxy terminating TLS is assumed and not provided. [deployment.md](deployment.md#reverse-proxy-and-tls) |

**Be direct with yourself about this section.** A platform with no rate
limiting, no abuse controls and no account lifecycle is not one to put in front
of the public without building those first. It is ready for an internal
deployment behind your own authentication perimeter; it is not ready to be a
public service, and nothing in this repository claims otherwise.

---

## Summary

| Stage | Position |
| --- | --- |
| **Evaluating** | Well supported. The checks are cheap and the boundaries are testable |
| **Internal deployment** | Supported, with real work on your side: secrets, backups, monitoring, scheduling, and the admin policy |
| **External exposure** | Not ready without building rate limiting, abuse controls and account lifecycle first |

**Related:** [evaluation.md](evaluation.md) · [deployment.md](deployment.md) ·
[extensions.md](extensions.md) · [SECURITY.md](../SECURITY.md)
