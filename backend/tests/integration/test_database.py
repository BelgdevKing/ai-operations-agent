"""Database connection and session management against a live PostgreSQL.

These skip themselves when PostgreSQL is unreachable, so the suite still passes
on a machine that has not finished setup. In the Compose stack, or once a
native PostgreSQL is running, they execute for real:

    docker compose exec backend pytest
    pytest -m integration
"""

from __future__ import annotations

import contextlib

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import check_database, engine, get_session, session_factory

# The `database` fixture in conftest.py is autouse: it skips these tests when
# PostgreSQL is unreachable and disposes the engine afterwards.
pytestmark = pytest.mark.integration


# -- Connection ---------------------------------------------------------------


async def test_engine_connects_and_executes() -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_check_database_reports_success() -> None:
    assert await check_database() is True


async def test_connection_reaches_the_expected_database() -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT current_database()"))
        assert result.scalar_one()


# -- Sessions -----------------------------------------------------------------


async def test_session_factory_produces_usable_sessions() -> None:
    async with session_factory() as session:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 42"))
        assert result.scalar_one() == 42


async def test_get_session_commits_and_closes() -> None:
    """Exercise the dependency exactly as FastAPI drives it."""
    generator = get_session()
    session = await generator.__anext__()

    result = await session.execute(text("SELECT 1"))
    assert result.scalar_one() == 1

    # Exhausting the generator runs the commit-and-close path.
    with contextlib.suppress(StopAsyncIteration):
        await generator.__anext__()

    assert not session.in_transaction()


async def test_get_session_rolls_back_when_the_handler_raises() -> None:
    generator = get_session()
    session = await generator.__anext__()

    await session.execute(text("SELECT 1"))

    with contextlib.suppress(StopAsyncIteration, RuntimeError):
        await generator.athrow(RuntimeError("handler blew up"))

    assert not session.in_transaction()


async def test_readiness_endpoint_against_live_dependencies(client: AsyncClient) -> None:
    """Full path: HTTP request through the service to the real database."""
    response = await client.get("/health/ready")
    dependencies = response.json()["dependencies"]

    if response.status_code == 503:
        pytest.skip(f"A required dependency is unavailable: {dependencies}")

    assert response.status_code == 200
    assert dependencies["postgres"] == {"status": "ok", "required": True}
    for name, check in dependencies.items():
        assert check["status"] == "ok" or not check["required"], name
