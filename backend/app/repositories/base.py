"""Generic repository.

Data access lives here so that services never build queries themselves. When
multi-tenancy lands, the tenant filter is added in one place - a subclass of
this - rather than in every call site, which is what makes the isolation
guarantee auditable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base


class BaseRepository[ModelT: Base]:
    """CRUD operations shared by every model.

    Subclasses bind the model:

        class TenantRepository(BaseRepository[Tenant]):
            model = Tenant
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Catch a missing binding at import time rather than at first query.
        if not hasattr(cls, "model") and not getattr(cls, "__abstract__", False):
            raise TypeError(f"{cls.__name__} must set a 'model' attribute")

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """Return one record by primary key, or None."""
        return await self.session.get(self.model, entity_id)

    async def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[ModelT]:
        """Return a page of records, newest first where a timestamp exists."""
        statement = select(self.model).limit(limit).offset(offset)
        created_at = getattr(self.model, "created_at", None)
        if created_at is not None:
            statement = statement.order_by(created_at.desc())
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def count(self) -> int:
        """Return the total number of records."""
        result = await self.session.execute(select(func.count()).select_from(self.model))
        return result.scalar_one()

    def add(self, entity: ModelT) -> ModelT:
        """Stage a new record.

        Not a coroutine and does not commit: the session opened for the request
        is the transaction boundary.
        """
        self.session.add(entity)
        return entity

    async def delete(self, entity_id: uuid.UUID) -> bool:
        """Delete by primary key. Returns whether a record was removed.

        Loads the row first so that ORM cascades and event listeners run; a
        bulk ``DELETE`` statement bypasses both.
        """
        entity = await self.get(entity_id)
        if entity is None:
            return False

        await self.session.delete(entity)
        return True

    async def flush(self) -> None:
        """Send staged changes to the database without committing.

        Use when the caller needs database-generated values before the request
        completes.
        """
        await self.session.flush()
