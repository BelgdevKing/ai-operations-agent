# Infrastructure

Docker Compose is the reference environment for the platform, and the target
for deployment. There is no Kubernetes and no orchestration beyond Compose.

Everything here stays supported even on machines that cannot run Docker.
Developers working natively instead follow
[docs/development-windows.md](../docs/development-windows.md); the two setups
share the same code and settings, differing only in hostnames and which `.env`
file is read.

## Layout

```
docker/
├── backend/     Dockerfile for the FastAPI service
└── frontend/    Dockerfile for the Next.js app
postgres/
└── init/        SQL run once, on first creation of the data volume
```

## Services

| Service | Image | Port | Status |
| --- | --- | --- | --- |
| postgres | `pgvector/pgvector:pg17` | 5432 | runnable |
| redis | `redis:7-alpine` | 6379 | runnable |
| backend | built from `docker/backend` | 8000 | `python:3.12-slim`, uvicorn `--reload` |
| frontend | built from `docker/frontend` | 3000 | `node:22-alpine`, `next dev` |

The pgvector image is used from the start so the RAG phase can add vector
columns without swapping the database image.

## Health checks

PostgreSQL and Redis are checked from `docker-compose.yml`, because their images
define none. The backend and frontend carry a `HEALTHCHECK` in their Dockerfile
instead, so the check travels with the image and applies to a plain `docker run`
as well. `docker compose ps` reports all four.

The backend waits for PostgreSQL and Redis to report healthy before it starts.

Compose sets `REDIS_REQUIRED=true`, so an unreachable Redis makes
`/health/ready` report 503 here. Native development leaves it `false`, where
Redis is not installed at all.

## Development configuration

Both application images are development images: source is bind-mounted from the
host and both servers reload on change. `node_modules` and `.next` live in
anonymous volumes so the container keeps its Linux-built dependencies rather
than the host's. File watching uses polling (`WATCHPACK_POLLING`), which is what
makes hot reload work across a Windows or macOS bind mount.

## Usage

```bash
docker compose up --build           # from the repository root
docker compose ps                   # status and health
docker compose logs -f backend
docker compose exec backend pytest
docker compose down                 # add -v to delete all data
```

`postgres/init/*.sql` runs **only** when the data volume is created. After
changing it, recreate the volume with `docker compose down -v`.
