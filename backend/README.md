# Backend

FastAPI modular monolith.

Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · Pydantic v2 · Alembic ·
PostgreSQL · Redis · pytest

**Implemented so far:** configuration, async database engine and session
management, the API router structure, exception handling, structured logging,
health endpoints, Alembic migrations, the full application schema,
authentication with multi-tenant authorization, and the provider-independent
LLM abstraction (types and interfaces only).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Service metadata |
| GET | `/health` | Liveness — answers without touching a dependency |
| GET | `/health/ready` | Readiness — 200 when every *required* dependency answers, else 503 |
| GET | `/docs` | OpenAPI UI |
| GET | `/api/v1/openapi.json` | OpenAPI schema |
| POST | `/api/v1/ai/generate` | Generate a completion (authenticated) |
| POST | `/api/v1/auth/register` | Create a user, organization and owner membership |
| POST | `/api/v1/auth/login` | Exchange credentials for an access token |
| GET | `/api/v1/auth/me` | The caller's profile and memberships |
| GET | `/api/v1/organization` | The organization the request acts on |
| GET | `/api/v1/organization/members` | List members |
| PATCH | `/api/v1/organization/members/{user_id}` | Change a member's role (admin+) |
| DELETE | `/api/v1/organization/members/{user_id}` | Remove a member (admin+, or yourself) |

Probes sit outside `/api/v1` on purpose: a health check should not have to
track an API version.

## Running natively (no Docker)

Requires Python 3.12 and a PostgreSQL install on `localhost`. Redis is **not**
required — see below.

```powershell
cd backend

py -3.12 -m venv .venv          # creates backend\.venv, git-ignored
.venv\Scripts\activate          # PowerShell or cmd.exe

pip install -r requirements-dev.txt

copy .env.example .env          # optional - defaults already target localhost

uvicorn app.main:app --reload
```

Activate the virtual environment in every new terminal before running `pip`,
`uvicorn`, `pytest` or `ruff`; the prompt shows `(.venv)` when it is active.
`deactivate` leaves it. In Git Bash use `source .venv/Scripts/activate`; if
PowerShell blocks the script, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

Database setup, environment files and troubleshooting:
[docs/development-windows.md](../docs/development-windows.md).

## Running in Docker

From the repository root:

```bash
docker compose up --build
docker compose exec backend pytest
```

## Tests and lint

With the virtual environment active:

```powershell
pytest                          # full suite
pytest tests\unit               # unit tests only, no database needed
pytest -m integration           # only the tests needing PostgreSQL
ruff check app tests            # lint
ruff format app tests           # format
mypy                            # type check
```

Every setting has a development default, so the suite runs with no environment
file. Integration tests skip themselves when PostgreSQL is unreachable.

## Dependencies

Redis is optional. No implemented feature uses it, connections are lazy, and
`REDIS_REQUIRED` defaults to `false` — so the backend starts, and
`/health/ready` returns 200, with Redis absent. The Compose stack sets
`REDIS_REQUIRED=true`, because there Redis really is part of the environment.

PostgreSQL is required: `/health/ready` returns 503 without it.

## Authentication

Passwords are hashed with **Argon2id** (`argon2-cffi`) and sessions carry a
**JWT** access token (`PyJWT`). Neither algorithm is implemented here; the code
supplies parameters and validation rules.

```
POST /api/v1/auth/register   create a user, their organization, and an owner membership
POST /api/v1/auth/login      exchange credentials for an access token
GET  /api/v1/auth/me         the caller's profile and memberships
```

Send the token as `Authorization: Bearer <token>`.

### What the token contains

A user id, issue and expiry times, a token type, and a unique id — nothing
else. A JWT is signed, not encrypted, so anything in it is readable by whoever
holds it.

**Role and organization are deliberately absent.** They are read from the
database on every request, so removing a member or reducing their role takes
effect on the next call rather than whenever their token happens to expire.

### Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `JWT_SECRET_KEY` | development key | **Staging and production refuse to start on the default.** |
| `JWT_ALGORITHM` | `HS256` | HMAC only — `HS256`, `HS384`, `HS512`. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | There is no revocation list yet; expiry is what ends a session. |
| `PASSWORD_MIN_LENGTH` | `12` | Length is the only rule. |
| `ARGON2_TIME_COST` | `3` | |
| `ARGON2_MEMORY_COST_KIB` | `65536` | 64 MiB, above the OWASP minimum of 19 MiB. |
| `ARGON2_PARALLELISM` | `4` | |

Generate a real secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Raising the Argon2 cost is safe: existing hashes keep working and are upgraded
on the owner's next successful login.

## Multi-tenancy and authorization

The organization a request acts on comes from the caller's **verified
membership**, never from the request. Callers in more than one organization
choose between them with the `X-Organization-ID` header — which is checked
against their memberships and grants nothing on its own. With a single
membership the header is unnecessary.

Dependencies in `app/api/deps.py`:

| Dependency | Gives |
| --- | --- |
| `CurrentUser` | the authenticated user |
| `CurrentMembership` | their verified membership of the organization in play |
| `CurrentOrganization` | that organization |
| `CurrentOrganizationId` | the tenant id, for scoping a repository |
| `require_role(role)` | refuses callers below that role |

Roles are ranked `member < admin < owner`, so `require_role(ADMIN)` admits
owners too.

| Role | May |
| --- | --- |
| owner | everything, including granting and revoking ownership |
| admin | manage members and organization resources; not owner-only actions |
| member | ordinary use; read the member list; not administer it |

Standing rules: nobody changes their own role, only an owner grants or revokes
ownership, and the last owner can be neither demoted nor removed.

## Tenant isolation

`TenantScopedRepository` in `app/repositories/tenant.py` is the single place the
organization filter is applied. Bind a tenant-owned model and every query it
makes carries `WHERE organization_id = :organization_id`:

```python
class AgentRepository(TenantScopedRepository[Agent]):
    model = Agent
```

- `get()` deliberately avoids `session.get`, which would fetch by primary key
  alone and return another tenant's row.
- `add()` stamps `organization_id` itself, so a service cannot create a row
  owned by somebody else.
- Binding a model with no `organization_id` raises at import.

A row belonging to another tenant reads as absent, not forbidden — whether an
id exists elsewhere is not the caller's business.

Row-level security is not implemented yet; it is the planned second layer
behind this one.

## AI abstraction

The application talks to `app/ai/`; `app/ai/` talks to vendors. Nothing else
imports a provider SDK.

```
POST /api/v1/ai/generate   app/api/v1/endpoints/ai.py
      ↓
  AIService           app/services/ai.py      ← application code calls this
      ↓
  LLMGateway          app/ai/gateway.py
      ↓
  LLMProvider         app/ai/providers/base.py
      ├── Anthropic   app/ai/providers/anthropic.py
      └── OpenAI      app/ai/providers/openai.py
```

**Application services use `LLMGateway` and never construct an adapter.** That
is what keeps provider choice a configuration change rather than a code change.

| Layer | Owns |
| --- | --- |
| Endpoint | Authentication, tenant context, request/response shape |
| `AIService` | Application policy: which models may be asked for, size limits, defaults |
| `LLMGateway` | Provider selection, retry policy and classification, backoff, correlation ids, observability, normalised operational behaviour |
| `LLMProvider` | Translating the request, calling the SDK, translating the response, translating the SDK's exceptions, provider-specific structured output |

Nothing above the gateway branches on which vendor is in use, and nothing below
it knows about retries.

### Why an abstraction rather than calling the SDK directly

- **Provider choice stays reversible.** Vendors change their pricing, their
  limits and their model line-ups. If `anthropic` were imported in fifty
  modules, changing provider would mean editing fifty modules. Here it is one
  package.
- **Tests need no network or API key.** Everything above `LLMProvider` is
  tested against a stub, so the suite stays fast, free and offline.
- **Failures have one shape.** Every SDK raises its own exception types. An
  adapter translates them into `app.ai.exceptions`, so callers catch `LLMError`
  rather than importing three vendors' error classes to handle "the call
  failed".
- **Cost and latency are recorded the same way whoever serves the call**, which
  is what makes per-tenant accounting possible later.

Two rules keep it honest, both enforced by tests:

1. No module outside `app/ai/providers/` may import a provider SDK.
2. No provider SDK exception may escape `app/ai/`.

### Types

| Type | Purpose |
| --- | --- |
| `LLMMessage` | One turn: `role` (`system`/`user`/`assistant`) and `content` |
| `LLMRequest` | `messages`, `model`, `temperature`, `max_output_tokens` |
| `LLMUsage` | `input_tokens`, `output_tokens`, and a derived `total_tokens` |
| `LLMResponse` | `content`, `provider`, `model`, `usage`, `latency_ms`, `request_id` |
| `LLMStructuredResponse[T]` | A response parsed and validated into a Pydantic model |

All are frozen: a request passes through the gateway to an adapter, and a layer
that mutated one would change what the caller believes it sent.

Two deliberate choices that look like omissions:

- **`LLMRequest.model` has no default.** A default would have to name a real
  model, which would make the shared types provider-dependent. The model comes
  from configuration at the layer that builds the request.
- **The system prompt is a message, not a field.** Providers disagree — some
  take a top-level parameter, some an ordinary message — so the shared shape
  keeps it as a message and each adapter rearranges it. `LLMRequest` exposes
  `system_messages` and `conversation_messages` for that.

### Errors

All derive from `LLMError`, which derives from `AppError`, so a failure that
reaches the API is rendered in the standard envelope with a correlation id.

| Exception | Status | Raised when |
| --- | --- | --- |
| `LLMConfigurationError` | 500 | No API key, unknown provider, disallowed model |
| `LLMAuthenticationError` | 500 | The provider rejected *our* credentials |
| `LLMRateLimitError` | 429 | Throttled; carries `retry_after_seconds` when known |
| `LLMTimeoutError` | 504 | The provider did not answer in time |
| `LLMProviderError` | 502 | Any other provider-side failure |
| `LLMInvalidResponseError` | 502 | Output unparseable, or failed schema validation |

**`LLMAuthenticationError` is 500, not 401.** In this application a 401 means
the caller's access token is bad. A provider refusing our key is a deployment
fault, and returning 401 would send a properly signed-in user to the login
screen because of a missing environment variable.

Status codes generally stay 5xx for the same reason: the caller's request was
fine, so a 4xx would wrongly tell them to change it. Rate limiting is the
exception, because retrying later genuinely is the right response.

### Providers

| | Anthropic | OpenAI |
| --- | --- | --- |
| SDK | `anthropic` | `openai` |
| Call | `messages.create` | `chat.completions.create` |
| System prompt | top-level `system` parameter | an ordinary message |
| Output cap | `max_tokens` | `max_completion_tokens` |
| Structured output | `messages.parse(output_format=...)` | `chat.completions.parse(response_format=...)` |
| `temperature` | omitted for models that removed sampling | sent only when explicitly set |

Both differences in the last row are real and would break calls if ignored.
Current Claude models (Opus 5, Sonnet 5, Opus 4.8/4.7, Fable/Mythos) **reject**
`temperature` with a 400, and the default model is one of them. OpenAI's
reasoning models reject a non-default temperature and the SDK gives no way to
enumerate which, so it is sent only when a caller asks for one.

Select one with `LLM_PROVIDER`; resolve it with
`app.ai.providers.registry.create_provider(settings)`. Nothing above the
registry branches on which vendor is in use.

### Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_TIMEOUT_SECONDS` | `60` | Ceiling on one call; no model request can hang |
| `ANTHROPIC_API_KEY` | *(none)* | Required in staging/production **if selected** |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `OPENAI_API_KEY` | *(none)* | Required in staging/production **if selected** |
| `OPENAI_MODEL` | `gpt-5.5` | |

**Only the selected provider needs a key.** Staging and production refuse to
start without it; development starts regardless and fails at call time with
`LLMConfigurationError`, so the application runs with no vendor account.

Keys are `SecretStr`: masked in reprs, logs, tracebacks and serialised
configuration. Reading one requires an explicit `.get_secret_value()`, which is
greppable in review.

### Gateway

```python
gateway = LLMGateway.from_settings(settings)          # application
gateway = LLMGateway(FakeProvider(), sleep=no_sleep)  # tests

response = await gateway.generate(request)
summary = await gateway.generate_structured(request, RefundSummary)
```

**Retries.** Only failures that could plausibly succeed on a second attempt:
`LLMRateLimitError`, `LLMTimeoutError`, and a transient `LLMProviderError`.
Never `LLMAuthenticationError`, `LLMConfigurationError` or
`LLMInvalidResponseError` — a wrong key, a wrong model name and output that
missed the schema all fail identically the second time.

A provider error can be either kind, so the status code on the chained cause
separates them: a 4xx other than 408/429 means the request itself is wrong and
is not retried. No SDK import is involved.

**Backoff** is exponential with full jitter — `uniform(0, min(base·2ⁿ, max))`.
Jitter matters: without it, every client that failed during an outage retries
in lockstep. The `sleep` and `jitter` functions are injectable, so tests never
wait.

**Rate limits.** A `retry-after` hint is used when it is no longer than
`LLM_RETRY_MAX_DELAY_SECONDS`. A longer one is honoured by *not* retrying
rather than by holding the caller for a minute; the error propagates with its
hint intact so the caller can decide.

**Attempt bound.** At most `LLM_MAX_RETRIES + 1` calls reach a provider — three
by default. The adapters build their SDK clients with `max_retries=0`, so the
gateway is the only retry layer; leaving the SDK's own retries on would have
made the worst case 3 × 3 = 9 calls.

**Latency** has two meanings, kept distinct:

| | Meaning | Where |
| --- | --- | --- |
| `LLMResponse.latency_ms` | The provider call itself | Set by the adapter, never rewritten |
| `gateway_latency_ms` | The whole operation, including retries and waits | Logged only |

So `generate()` returns the adapter's response object **unchanged**.

**Observability.** Every call carries an `llm_call_id`, and the log formatter
already attaches the ambient HTTP `request_id`, so a model call can be traced
back to the request that caused it. Logged: provider, model, served model,
attempt number, outcome, token counts, both latencies, normalised error type.

**Never logged: prompts or completions.** They are the tenant's business data,
not operational metadata. Credentials cannot appear because the gateway never
holds one — the adapter passes the key straight to its SDK client. Both are
enforced by tests. Content logging, if it is ever wanted, must be an explicit
opt-in.

### Gateway configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_MAX_RETRIES` | `2` | Retries after the first attempt; `0` disables |
| `LLM_RETRY_MAX_DELAY_SECONDS` | `8` | Caps any single wait, and the longest `retry-after` worth honouring |
| `LLM_ALLOWED_MODELS` | *(empty)* | Comma-separated models a request may name. Empty means the configured model only |

### The AI endpoint

```
POST /api/v1/ai/generate
Authorization: Bearer <token>

{"messages": [{"role": "user", "content": "Explain what an AI operations agent is."}]}
```

```json
{
  "content": "An AI operations agent carries out business tasks under supervision.",
  "model": "claude-opus-5",
  "usage": {"input_tokens": 42, "output_tokens": 17, "total_tokens": 59},
  "latency_ms": 412.7
}
```

**Authentication is required** — the existing Part 9 system, no second
mechanism. The organization the call is attributed to comes from the caller's
verified membership; the request has no field that could name one, and an
inactive user, membership or organization is refused before the service runs.

**Provider selection is the server's.** A request cannot name a provider —
`extra="forbid"` means `{"provider": "openai"}` is a 422 rather than silently
ignored. `model` is optional and normally omitted; naming one works only if the
deployment allowed it via `LLM_ALLOWED_MODELS`, which defaults to the configured
model alone.

Limits, all conservative and enforced at the schema:

| Limit | Value |
| --- | --- |
| Messages per request | 50 |
| Characters per message | 10,000 |
| Characters per conversation | 50,000 |
| `max_output_tokens` | 1–4,096 |
| `temperature` | 0.0–2.0 |

The response carries no provider name and no provider request id. The model
identifier does imply the vendor — callers need to know what produced the text
— but naming the provider would make a routing detail part of the contract, and
`X-Request-ID` already gives a caller something to quote.

**Errors** arrive already normalised: 429 rate limited, 504 timeout, 502
provider fault or unusable output, 500 misconfiguration. Each is the standard
envelope with a correlation id. A 5xx never carries the specific message — it
would name environment variables, models and providers — only the generic one
for its error code; the detail is in the log against the same correlation id.

Application code calls `AIService`, which calls `LLMGateway`. Nothing outside
`app/ai/providers` touches a vendor, enforced per layer by a test.

### Built in stages

Part 10 is incremental. **10A** was the foundation — shared types, errors, the
`LLMProvider` interface. **10B** added the Anthropic and OpenAI adapters and a
registry. **10C** added the `LLMGateway`. **10D**, complete, adds `AIService`
and the first authenticated endpoint. Nothing is persisted yet: there is no
conversation storage.

Importing `app.ai` deliberately does **not** import a vendor SDK — the adapters
live under `app.ai.providers` so code needing only the shared types does not
pay for two SDKs it will not call. A test runs a fresh interpreter to prove it.

## Layout

```
app/
├── main.py         application factory, middleware, router mounting
├── core/           config, database, cache, logging, middleware, exceptions,
│                   security (Argon2id + JWT)
├── api/            routers; deps.py wires dependencies, v1/ is the versioned API
├── models/         SQLAlchemy models: Base, mixins
├── schemas/        Pydantic v2 request/response contracts
├── repositories/   data access; tenant.py holds the tenant-scoping pattern
├── services/       business logic
├── ai/             provider-independent LLM abstraction
│   └── providers/  one adapter per vendor; only these may import an SDK
├── agents/         Claude tool-use loop                      (not built yet)
├── tools/          tool registry                             (not built yet)
├── workflows/      workflow definitions and state machine    (not built yet)
├── knowledge/      document ingestion and retrieval          (not built yet)
└── audit/          append-only audit trail                   (not built yet)
alembic/            migration environment and versions
tests/              unit/ and integration/
```

## Layer rules

| Layer | Does | Must not |
| --- | --- | --- |
| `api/` | Validate input, call a service, shape the response | Contain business logic or queries |
| `services/` | The business operation; raise domain errors | Import FastAPI or write SQL |
| `repositories/` | Build and run queries | Contain business rules |
| `models/` | Define tables | Know about HTTP |

Services never import FastAPI. They raise `app.core.exceptions` errors and the
API layer translates them, which is what lets a service be reused from a worker
or an agent tool rather than only from an HTTP request.

## Error handling

Every error leaves the API in one envelope, carrying the correlation id that
also appears in the logs and the `X-Request-ID` response header:

```json
{
  "error": {"code": "not_found", "message": "...", "details": {}},
  "request_id": "c8546e48c46b46719f6f8e3ae86e4b58"
}
```

Raise the classes in `app/core/exceptions.py` — `NotFoundError`,
`ConflictError`, `ValidationError`, `UnauthorizedError`,
`PermissionDeniedError`, `ServiceUnavailableError` — and the status code and
error code follow. An unhandled exception is logged with its traceback and
returned as a generic 500, never with internals in the body.

## Logging

`LOG_FORMAT=console` (default) gives readable lines; `LOG_FORMAT=json` gives one
JSON object per line for aggregators. Both carry the request's correlation id.
Attach structured fields with `extra`:

```python
logger.info("Tenant created", extra={"context": {"tenant_id": str(tenant.id)}})
```

## Migrations

Run from this directory, with the virtual environment active:

```powershell
alembic revision --autogenerate -m "add tenants"   # generate from model changes
alembic upgrade head                               # apply
alembic downgrade -1                               # undo the last one
alembic current                                    # what is applied
alembic check                                      # fail if models and schema differ
alembic upgrade head --sql                         # emit SQL without a database
```

Alembic reads `DATABASE_URL` from the application settings, so migrations and
the application can never point at different databases. A new model must be
imported in `app/models/__init__.py` or autogenerate will not see it.

## Rules

- No SQL in services, no business logic in routers.
- Every tenant-owned query goes through a tenant-scoped repository.
- Secrets come from environment variables, never from code.
- New tables ship with an Alembic migration in the same change.
- Every function is annotated; `mypy` runs with `disallow_untyped_defs`.

See [../docs/architecture.md](../docs/architecture.md).
