"""Async SQLAlchemy engine and session management.

The engine is created once per process and opens connections lazily, so
importing this module never touches the network and the application starts with
PostgreSQL down.

Transaction policy: ``get_session`` commits when the request handler returns
normally and rolls back if it raises, so a request is one unit of work and
services never manage transactions themselves.

Failure policy: a request that could not reach PostgreSQL at all is answered
``503 service_unavailable`` rather than ``500 internal_error``. The two are not
the same operational event - one says "try again shortly", the other says "this
service has a bug" - and a client, a load balancer and an on-call engineer all
act differently on them. The translation lives here, where the driver's
exception types already do, so nothing above this module learns what SQLAlchemy
raises.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.exceptions import ServiceUnavailableError

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
        # Two bounds the pool does not provide. `pool_timeout` only limits how
        # long a caller waits for an *existing* connection to come free; when
        # the server is unreachable, or reachable and no longer answering,
        # nothing else in the stack ends the wait. Without these the limit is
        # the operating system's, which on a path that drops packets rather
        # than refusing them is measured in minutes.
        #
        # Alembic does not go through this function - it builds its own engine
        # in alembic/env.py - so a long migration cannot be cut off by the
        # command timeout below.
        connect_args={
            "timeout": settings.database_connect_timeout_seconds,
            "command_timeout": settings.database_command_timeout_seconds,
        },
    )


engine: AsyncEngine = create_engine()

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# Failures that mean the database could not serve the request at all, rather
# than that the request was wrong. `OSError` is the socket - refused, reset, no
# route, DNS - and `TimeoutError` is what the driver raises when the command
# timeout fires on a server that accepted the connection and then went quiet.
_TRANSPORT_FAILURES: Final = (OSError, TimeoutError)


def is_unavailable(exc: BaseException) -> bool:
    """Whether this failure means PostgreSQL could not serve the request.

    Deliberately narrow, because the cost of being wrong runs both ways. Too
    broad and a genuine bug - a constraint violated, a query that does not
    compile - is reported as "try again shortly" and nobody investigates it.
    Too narrow and a database outage looks like an application fault.

    So: the two SQLAlchemy classes that mean the connection itself failed, any
    DBAPI error that invalidated its connection, and the raw socket and timeout
    errors that escape before SQLAlchemy has wrapped anything. Notably *not*
    ``IntegrityError`` or ``ProgrammingError`` - those are this application's
    problem and stay a 500.
    """
    if isinstance(exc, InterfaceError | OperationalError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, _TRANSPORT_FAILURES)


async def _rollback_quietly(session: AsyncSession) -> None:
    """Roll back, and do not let the attempt replace the real failure.

    Rolling back over a connection that has already gone raises again, and that
    second exception would be the one the caller saw - so the request would be
    reported as whatever the rollback hit rather than as what actually went
    wrong. The session is discarded either way: the context manager closes it,
    and a connection that failed here is not returned to the pool for the next
    request to inherit.
    """
    try:
        await session.rollback()
    except Exception:
        logger.debug("Rollback failed; the original error stands", exc_info=True)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session bound to the request.

    Commits on success, rolls back on failure, always closes - and translates
    "the database was not there" into the application's own
    ``service_unavailable`` rather than letting it arrive as an unhandled
    exception. Everything else is re-raised exactly as it was raised, so a real
    defect still surfaces as one.
    """
    async with session_factory() as session:
        try:
            yield session
        except Exception as exc:
            await _rollback_quietly(session)
            if is_unavailable(exc):
                logger.warning("PostgreSQL is unavailable: %s", type(exc).__name__)
                raise ServiceUnavailableError() from exc
            raise
        else:
            # The commit is part of the request too. A database that went away
            # between the last read and here fails exactly the same way, and
            # would otherwise escape this translation entirely.
            try:
                await session.commit()
            except Exception as exc:
                await _rollback_quietly(session)
                if is_unavailable(exc):
                    logger.warning("PostgreSQL is unavailable: %s", type(exc).__name__)
                    raise ServiceUnavailableError() from exc
                raise


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
