# Infrastructure

Docker Compose is the reference environment for the platform, and the target
for deployment. There is no Kubernetes and no orchestration beyond Compose.

Everything here stays supported even on machines that cannot run Docker.
Developers working natively instead follow
[docs/development-windows.md](../docs/development-windows.md); the two setups
share the same code and settings, differing only in hostnames and which `.env`
file is read.

**Deploying is [docs/deployment.md](../docs/deployment.md).** This file
describes what is here and how the two stacks differ; that one is the runbook.

## Layout

```
docker/
├── backend/     Dockerfile for the FastAPI service   (development | production)
└── frontend/    Dockerfile for the Next.js app       (development | builder | production)
postgres/
└── init/        SQL run once, on first creation of the data volume
```

At the repository root:

```
docker-compose.yml            local development stack
docker-compose.prod.yml       deployed stack
.env.example                  development template
.env.production.example       deployment template, placeholders only
```

## Two targets, one Dockerfile per service

Each Dockerfile builds both images, so the base image, the language version and
the unprivileged user cannot drift apart between development and production.

| | `development` | `production` |
| --- | --- | --- |
| backend deps | `requirements-dev.txt` (pytest, mypy, ruff) | `requirements.txt` only |
| backend source | bind-mounted from the host | copied into the image |
| backend server | `uvicorn --reload` | `uvicorn`, `--proxy-headers`, 30s graceful shutdown, no access log, no server header |
| backend health | `/health` via `curl` | `/health/ready` via Python - a container that has lost PostgreSQL stops being healthy |
| frontend | `npm ci`, `next dev`, polling file watcher | `output: "standalone"` server, no npm tree, no source, no toolchain |
| runs as | `appuser` / `node` | `appuser` / `node` |

`production` is the **last** stage in both files, so a bare `docker build` with
no `--target` produces the hardened image rather than the development one. Both
Compose files name their target explicitly regardless.

## Services

| Service | Image | Development | Deployment |
| --- | --- | --- | --- |
| postgres | `pgvector/pgvector:pg17` | published on 5432 | no host port, 60s stop grace, data checksums |
| redis | `redis:7-alpine` | published on 6379 | no host port, bounded memory, `noeviction` |
| migrate | backend image | – | one shot, `alembic upgrade head`, must exit 0 first |
| backend | built from `docker/backend` | published on 8000 | `127.0.0.1:8000` only |
| frontend | built from `docker/frontend` | published on 3000 | `127.0.0.1:3000` only |

The pgvector image is used from the start so the RAG phase can add vector
columns without swapping the database image.

## Health checks

PostgreSQL and Redis are checked from the Compose files, because their images
define none. The backend and frontend carry a `HEALTHCHECK` in their Dockerfile
instead, so the check travels with the image and applies to a plain
`docker run` as well. `docker compose ps` reports all of them.

The backend waits for PostgreSQL and Redis to be healthy before it starts, and
in the deployed stack it also waits for `migrate` to have exited successfully.

Compose sets `REDIS_REQUIRED=true`, so an unreachable Redis makes
`/health/ready` report 503 in both stacks. Native development leaves it
`false`, where Redis is not installed at all.

## Development configuration

Both development images bind-mount source from the host and reload on change.
`node_modules` and `.next` live in anonymous volumes so the container keeps its
Linux-built dependencies rather than the host's. File watching uses polling
(`WATCHPACK_POLLING`), which is what makes hot reload work across a Windows or
macOS bind mount.

## Observability toggles

Both stacks pass the same four settings through, and both default them off:

| Setting | Effect |
| --- | --- |
| `METRICS_ENABLED` / `METRICS_TOKEN` | Serves `GET /metrics`. There is no unauthenticated mode - enabling it without a token refuses to start, in every environment. |
| `TRACING_ENABLED` / `TRACING_SAMPLE_RATIO` | One span per request, plus one per agent run, model call, tool execution, workflow step and approval decision, written to the log in OpenTelemetry's shape. |
| `DOCS_ENABLED` | `/docs`, `/redoc` and the OpenAPI schema. On in development, off in the deployed stack. |

Turning tracing on locally is the quickest way to see what a span carries -
which is a route template, a lifecycle state and a token count, and nothing
belonging to a tenant.

## Deployment configuration

The deployed stack is a separate file rather than an overlay, so reading it
tells you what runs without having to work out which values from
`docker-compose.yml` survived.

What is different, and why, in one line each:

- **No source is mounted.** A production container runs the code in its image.
- **No development defaults.** Every secret is `${VAR:?...}`, so Compose
  refuses to start - naming the variable - rather than falling back to a value
  published in this repository.
- **The database and cache have no host ports.** They are reachable on the
  internal network and nowhere else.
- **The applications publish to loopback.** A reverse proxy terminates TLS.
- **Migrations are their own service.** The application never migrates on
  start-up: N containers starting together would be N concurrent
  `alembic upgrade head` runs against one database.
- **`no-new-privileges`, all capabilities dropped, memory limits, bounded log
  files.**

## Usage

```bash
# Development, from the repository root
docker compose up --build
docker compose ps
docker compose logs -f backend
docker compose exec backend pytest
docker compose down                 # add -v to delete all data

# Deployment - see docs/deployment.md for the whole procedure
cp .env.production.example .env.production   # then fill it in
docker compose --env-file .env.production -f docker-compose.prod.yml up -d
```

`postgres/init/*.sql` runs **only** when the data volume is created. After
changing it, recreate the volume with `docker compose down -v`.
