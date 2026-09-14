# =============================================================================
# Backend - FastAPI on Python 3.12
#
# Development image: dependencies include the test tooling and the server runs
# with --reload against the bind-mounted source in docker-compose.yml.
#
# Build context is ./backend (see docker-compose.yml).
# =============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is used by the container HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so edits to application code do not invalidate this layer.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --upgrade pip && pip install -r requirements-dev.txt

COPY . .

# Run as an unprivileged user. The uid is pinned so bind-mounted files keep
# sensible ownership on Linux hosts, falling back if 1000 is already taken.
RUN useradd --create-home --uid 1000 appuser || useradd --create-home appuser
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
