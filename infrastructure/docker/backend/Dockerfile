# =============================================================================
# Backend - FastAPI on Python 3.12
#
# Two targets from one file, so the development image and the production image
# cannot drift apart in the parts that matter - the base, the Python version,
# the unprivileged user:
#
#   development   test tooling installed, source bind-mounted, --reload
#   production    runtime dependencies only, source baked in, no reloader
#
# Built with an explicit target from each Compose file:
#
#   docker compose build backend                            -> development
#   docker compose -f docker-compose.prod.yml build backend -> production
#
# `production` is last, so a bare `docker build` with no --target produces the
# production image rather than accidentally shipping the development one.
#
# Build context is ./backend (see either Compose file).
# =============================================================================

# -- Shared base ---------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as an unprivileged user in both targets. The uid is pinned so that
# bind-mounted files keep sensible ownership on Linux hosts, falling back if
# 1000 is already taken by the base image.
RUN useradd --create-home --uid 1000 appuser || useradd --create-home appuser


# -- Development ---------------------------------------------------------------
FROM base AS development

# curl is for the health check below. It is installed in this target only:
# the production image has no use for an HTTP client on the filesystem, and
# what is not installed cannot be used by anybody who gets a shell.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so edits to application code do not invalidate this layer.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements-dev.txt

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# -- Production ----------------------------------------------------------------
FROM base AS production

# requirements.txt, not requirements-dev.txt. pytest, mypy and ruff are build
# tools; an image that carries them is a larger image with more to audit and
# nothing more to offer at runtime.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Named directories rather than `COPY . .`, so the image contains what it runs
# and nothing else: the application, the migration chain that the migrate job
# applies, and the two maintenance scripts an operator schedules. Not the test
# suite, not the tooling configuration, not the README. `--chown` during the
# copy rather than a chown afterwards, because a second RUN would duplicate
# every file in a new layer.
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser alembic.ini ./alembic.ini

USER appuser

EXPOSE 8000

# Readiness, not liveness. A container that has lost PostgreSQL is not a
# container a load balancer should be sending requests to, and /health/ready is
# the endpoint that already knows the difference - it answers 503 while a
# required dependency is unreachable and 200 when it is not.
#
# Python rather than curl, because Python is already here and curl is not.
# start-period is generous: the process must come up, and the database must be
# reachable, before the first failure is allowed to count.
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=4 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"]

# One worker per container by default, and that is a decision rather than an
# oversight - see docs/deployment.md. The metrics registry is per process, so a
# scrape of a multi-worker container reports whichever worker answered, and the
# honest way to scale is more containers, each scraped in its own right.
# WEB_CONCURRENCY overrides it for anybody who has read that section.
#
#   --proxy-headers          trust X-Forwarded-Proto/For, so redirects and
#                            client addresses are right behind a reverse proxy.
#                            Which senders are trusted is FORWARDED_ALLOW_IPS,
#                            set in the manifest rather than baked in here.
#   --timeout-graceful-shutdown 30
#                            on SIGTERM, stop accepting, let in-flight requests
#                            finish, then run the lifespan shutdown that closes
#                            the pools. An agent step mid-flight gets 30s.
#   --no-server-header       do not announce the server and its version.
#   --no-access-log          the request-context middleware already logs one
#                            line per request, with the correlation id on it.
#                            uvicorn's line is the same request again.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--timeout-graceful-shutdown", "30", \
     "--no-server-header", \
     "--no-access-log"]
