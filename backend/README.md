# Backend

FastAPI modular monolith.

Python 3.12 · FastAPI · SQLAlchemy 2.x (async) · Pydantic v2 · PostgreSQL ·
Redis · pytest

**Implemented so far:** settings, async database engine, Redis client, health
endpoints, test harness. The `modules/` packages are still empty.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Service metadata |
| GET | `/health` | Liveness — answers without touching a dependency |
| GET | `/health/ready` | Readiness — 200 when every *required* dependency answers, else 503 |
| GET | `/docs` | OpenAPI UI |

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
pytest tests\unit               # unit tests only
ruff check app tests            # lint
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
├── core/       settings, database engine and session, security primitives, logging
├── api/v1/     versioned routers and dependency wiring
├── shared/     pagination, error types, base schemas, model mixins
├── workers/    background job handlers (Redis-backed queue)
└── modules/    business modules - one package per bounded area
alembic/        migration environment and versions
tests/          unit/ and integration/
```

## Module convention

Every package under `modules/` has the same shape:

| File | Responsibility |
| --- | --- |
| `router.py` | HTTP routes; no business logic |
| `schemas.py` | Pydantic v2 request/response models |
| `service.py` | Business logic and the module public interface |
| `repository.py` | Data access; every query scoped by `tenant_id` |
| `models.py` | SQLAlchemy ORM models |
| `exceptions.py` | Domain errors, mapped to HTTP at the API edge |

Modules call each other through `service.py` only — never by importing another
module models or querying its tables.

## Rules

- No SQL in services, no business logic in routers.
- Every tenant-owned query goes through a tenant-scoped repository.
- Secrets come from environment variables, never from code.
- New tables ship with an Alembic migration in the same change.

See [../docs/architecture.md](../docs/architecture.md).
