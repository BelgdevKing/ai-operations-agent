# Infrastructure

Local development runs on Docker Compose. There is no Kubernetes and no
orchestration beyond Compose.

## Layout

```
docker/
├── backend/     Dockerfile for the FastAPI service (placeholder)
└── frontend/    Dockerfile for the Next.js app (placeholder)
postgres/
└── init/        SQL run once, on first creation of the data volume
```

## Services

| Service | Image | Port | Status |
| --- | --- | --- | --- |
| postgres | `pgvector/pgvector:pg17` | 5432 | runnable |
| redis | `redis:7-alpine` | 6379 | runnable |
| backend | built from `docker/backend` | 8000 | placeholder, commented out |
| frontend | built from `docker/frontend` | 3000 | placeholder, commented out |

The pgvector image is used from the start so the RAG phase can add vector
columns without swapping the database image.

## Usage

```bash
cp .env.example .env            # from the repository root
docker compose up -d postgres redis
docker compose ps
docker compose logs -f postgres
docker compose down             # add -v to delete all data
```

`postgres/init/*.sql` runs **only** when the data volume is created. After
changing it, recreate the volume with `docker compose down -v`.
