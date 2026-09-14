"""Async SQLAlchemy engine and session management.

The engine is created once per process and opens connections lazily, so
importing this module never touches the network and the application starts with
PostgreSQL down.

Transaction policy: ``get_session`` commits when the request handler returns
normally and rolls back if it raises, so a request is one unit of work and
services never manage transactions themselves.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build an engine from settings. Separate from the module-level engine so
    tests and Alembic can construct their own."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
    )


engine: AsyncEngine = create_engine()

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session bound to the request.

    Commits on success, rolls back on failure, always closes.
    """
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def check_database() -> bool:
    """Return True when PostgreSQL answers a trivial query.

    Never raises: an unreachable database is a reported state, not an error.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        # Readiness is polled continuously; keep the traceback at debug level.
        logger.warning("PostgreSQL is unavailable: %s", exc)
        logger.debug("PostgreSQL readiness check traceback", exc_info=True)
        return False
    return True


async def dispose_engine() -> None:
    """Close all pooled connections on shutdown."""
    await engine.dispose()
