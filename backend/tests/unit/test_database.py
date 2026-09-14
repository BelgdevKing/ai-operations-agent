"""Database wiring that can be verified without a live PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import database as db
from app.core.config import Settings

UNREACHABLE_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"


def test_engine_is_built_lazily() -> None:
    """Importing the module must not open a connection, or the application
    could not start with PostgreSQL down."""
    assert isinstance(db.engine, AsyncEngine)
    assert db.engine.pool.checkedout() == 0


def test_engine_honours_pool_settings() -> None:
    engine = db.create_engine(Settings(database_pool_size=7, database_max_overflow=3))

    assert engine.pool.size() == 7


def test_sessions_do_not_expire_on_commit() -> None:
    """Expired attributes would trigger lazy loads after the request's commit,
    which cannot work once the session is closed."""
    assert db.session_factory.kw["expire_on_commit"] is False


async def test_session_factory_produces_async_sessions() -> None:
    async with db.session_factory() as session:
        assert isinstance(session, AsyncSession)


async def test_check_database_returns_false_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable database is a reported state, never an exception -
    otherwise the readiness probe itself would fail."""
    unreachable = db.create_engine(Settings(database_url=UNREACHABLE_URL))
    monkeypatch.setattr(db, "engine", unreachable)

    try:
        assert await db.check_database() is False
    finally:
        await unreachable.dispose()


async def test_dispose_engine_is_safe_without_connections() -> None:
    """Shutdown runs even when nothing ever connected."""
    engine = db.create_engine(Settings(database_url=UNREACHABLE_URL))

    await engine.dispose()
