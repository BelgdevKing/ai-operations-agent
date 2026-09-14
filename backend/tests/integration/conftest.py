"""Fixtures for tests that talk to a real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@pytest.fixture
async def session(database: None) -> AsyncGenerator[AsyncSession, None]:
    """A session whose writes are always rolled back.

    The session joins an outer transaction that is never committed, so tests
    can insert freely - and call ``commit()`` - without leaving anything
    behind. ``join_transaction_mode="create_savepoint"`` is what makes an inner
    ``commit()`` release a savepoint instead of ending the outer transaction.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )

        async with factory() as db_session:
            yield db_session

        await transaction.rollback()
