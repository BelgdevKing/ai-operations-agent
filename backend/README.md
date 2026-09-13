# Backend

FastAPI modular monolith. **Not implemented yet** — this directory currently
holds the target structure only.

Python 3.14 · FastAPI · SQLAlchemy 2.x (async) · Pydantic v2 · Alembic ·
PostgreSQL · Redis · pytest

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
