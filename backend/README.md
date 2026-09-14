# Backend

FastAPI modular monolith.

Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · Pydantic v2 · Alembic ·
PostgreSQL · Redis · pytest

**Implemented so far:** configuration, async database engine and session
management, the API router structure, exception handling, structured logging,
health endpoints, Alembic migrations, the full application schema, and
authentication with multi-tenant authorization.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Service metadata |
| GET | `/health` | Liveness — answers without touching a dependency |
| GET | `/health/ready` | Readiness — 200 when every *required* dependency answers, else 503 |
| GET | `/docs` | OpenAPI UI |
| GET | `/api/v1/openapi.json` | OpenAPI schema |
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
