"""Fixtures for tests that talk to a real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.core.database import check_database, engine


@pytest.fixture(autouse=True)
async def database() -> AsyncGenerator[None, None]:
    """Require a reachable PostgreSQL, and hand the engine back clean.

    The engine is process-global, but an asyncpg connection is bound to the
    event loop that opened it and pytest gives each test its own loop. A pooled
    connection inherited by the next test therefore belongs to a loop that has
    already closed, which deadlocks rather than failing. Disposing after every
    test keeps each one self-contained.
    """
    if not await check_database():
        await engine.dispose()
        pytest.skip("PostgreSQL is not reachable")

    yield

    await engine.dispose()
