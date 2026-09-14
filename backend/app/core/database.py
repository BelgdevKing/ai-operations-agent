"""Async SQLAlchemy engine and session management.

No models or migrations yet - this provides the connection plumbing and the
readiness check used by ``GET /health/ready``.
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

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# Connections are opened lazily, so importing this module never touches the network.
engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    async with session_factory() as session:
        yield session


async def check_database() -> bool:
    """Return True when PostgreSQL answers a trivial query."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        # Readiness is polled continuously; keep the traceback at debug level.
        logger.warning("PostgreSQL readiness check failed: %s", exc)
        logger.debug("PostgreSQL readiness check traceback", exc_info=True)
        return False
    return True


async def dispose_engine() -> None:
    """Close all pooled connections on shutdown."""
    await engine.dispose()
