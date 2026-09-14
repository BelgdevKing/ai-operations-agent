# Backend

FastAPI modular monolith.

Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · Pydantic v2 · Alembic ·
PostgreSQL · Redis · pytest

**Implemented so far:** configuration, async database engine and session
management, the API router structure, exception handling, structured logging,
health endpoints, Alembic migrations and the test harness. No business
resources yet - tenants are the first.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Service metadata |
| GET | `/health` | Liveness — answers without touching a dependency |
| GET | `/health/ready` | Readiness — 200 when every *required* dependency answers, else 503 |
| GET | `/docs` | OpenAPI UI |
| GET | `/api/v1/openapi.json` | OpenAPI schema |

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

## Layout

```
app/
├── main.py         application factory, middleware, router mounting
├── core/           config, database, cache, logging, middleware, exceptions
├── api/            routers; deps.py wires dependencies, v1/ is the versioned API
├── models/         SQLAlchemy models: Base, mixins
├── schemas/        Pydantic v2 request/response contracts
├── repositories/   data access
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
