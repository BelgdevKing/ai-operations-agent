"""Application entry point.

``create_app`` is a factory rather than a module-level singleton so tests can
build an application with their own settings. ``app`` is the instance uvicorn
serves.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import router as api_v1_router
from app.core import cache, database
from app.core.config import Settings, get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.observability.instruments import Instruments
from app.observability.tracing import build_tracer

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Start-up and shut-down.

    Nothing here connects to a dependency: the engine and the Redis client open
    connections lazily, so the application starts even when PostgreSQL or Redis
    is down and reports the fact on ``/health/ready`` instead of refusing to
    boot.
    """
    settings: Settings = app.state.settings

    logger.info(
        "Starting %s",
        settings.app_name,
        extra={
            "context": {
                "version": __version__,
                "environment": settings.app_env,
                "api_prefix": settings.api_v1_prefix,
            }
        },
    )

    yield

    # Reached once the server has stopped accepting connections and the
    # in-flight requests it was given have finished - uvicorn runs the lifespan
    # shutdown after that drain, which is what makes closing the pools here
    # safe. An operator watching a rolling restart sees this line, then
    # "Shutdown complete"; a gap between them is a connection that would not
    # close, and that is worth being able to see.
    logger.info("Shutting down %s", settings.app_name)

    await database.dispose_engine()
    await cache.close_redis()
    logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application: logging, middleware, error handlers, routes."""
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    # The generated documentation is a complete map of the API: every route,
    # every field, every error code. That is the right thing to publish while
    # somebody is building against it and a thing to decide about deliberately
    # once the deployment is reachable from anywhere, so it is one setting and
    # the production manifest turns it off. Off means off: no schema either,
    # because /docs is only a renderer for it.
    docs_url = "/docs" if settings.docs_enabled else None
    redoc_url = "/redoc" if settings.docs_enabled else None
    openapi_url = f"{settings.api_v1_prefix}/openapi.json" if settings.docs_enabled else None

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Multi-tenant AI operations agent platform.",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    # Note: Starlette's debug flag is deliberately NOT wired to settings.debug.
    # In debug mode Starlette answers unhandled exceptions with a raw traceback
    # and never calls the registered 500 handler, which would both bypass the
    # error envelope and leak internals. The traceback goes to the log instead.
    app.state.settings = settings

    # Per application, not per process. Two applications in one process - which
    # is every integration test - therefore measure independently, and
    # "this counter incremented exactly once" stays a statement a test can make.
    app.state.instruments = Instruments()

    # Per application for the same reason, and off unless a deployment asked
    # for it. The resource attributes are the only thing every span carries;
    # all three are deployment configuration, none of them is a tenant.
    app.state.tracer = build_tracer(
        enabled=settings.tracing_enabled,
        ratio=settings.tracing_sample_ratio,
        resource={
            "service.name": settings.app_name,
            "service.version": __version__,
            "deployment.environment.name": settings.app_env,
        },
    )

    # Starlette applies the LAST middleware added as the outermost layer, so
    # RequestContextMiddleware goes on after CORS: every request then gets a
    # correlation id, including CORS preflights and anything CORS rejects.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        RequestContextMiddleware,
        instruments=app.state.instruments,
        tracer=app.state.tracer,
    )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)

    @app.get("/", tags=["meta"], summary="Service metadata")
    async def root() -> dict[str, str]:
        metadata = {
            "service": settings.app_name,
            "version": __version__,
            "environment": settings.app_env,
            "health": "/health",
            "api": settings.api_v1_prefix,
        }
        # Advertised only when it is actually there. A pointer to a 404 is
        # worse than no pointer: it sends somebody looking for a proxy problem.
        if docs_url:
            metadata["docs"] = docs_url
        return metadata

    return app


app = create_app()
