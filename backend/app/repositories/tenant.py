"""Tenant-scoped repository.

The single place the organization filter is applied. Every query a subclass
makes carries ``WHERE organization_id = :organization_id``, so isolation is a
property of this class rather than something each call site has to remember -
which is what makes it auditable: to check that tenant data cannot leak, read
this file and confirm that services only reach the database through it.

The organization id comes from the caller's verified membership (see
``app.api.deps``), never from a request body, query string or path.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base
from app.repositories.base import BaseRepository

TENANT_COLUMN = "organization_id"


class TenantScopedRepository[ModelT: Base](BaseRepository[ModelT]):
    """Data access confined to one organization.

    Subclasses bind a model that carries ``OrganizationScopedMixin``:

        class AgentRepository(TenantScopedRepository[Agent]):
            model = Agent
    """

    # Binds no model itself; concrete subclasses must.
    __abstract__ = True

    def __init__(self, session: AsyncSession, organization_id: uuid.UUID) -> None:
        super().__init__(session)
        self.organization_id = organization_id

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        # Checked against the mapped table rather than against a mixin: what
        # matters is that the column exists to filter on, not how it was
        # declared. Without it, scoping would silently do nothing - so fail at
        # import rather than leak at runtime.
        if model is not None and TENANT_COLUMN not in model.__table__.c:
            raise TypeError(
                f"{cls.__name__} scopes {model.__name__}, which has no "
                f"{TENANT_COLUMN} column; use BaseRepository instead."
            )

    @property
    def tenant_filter(self) -> ColumnElement[bool]:
        """The predicate every query in this repository must include."""
        # Guaranteed by __init_subclass__ above.
        return self.model.organization_id == self.organization_id  # type: ignore[attr-defined]

    def scoped(self, statement: Select[Any]) -> Select[Any]:
        """Confine a select to this organization.

        Subclasses building their own queries must pass them through here.
        """
        return statement.where(self.tenant_filter)

    def select(self) -> Select[tuple[ModelT]]:
        """A select over this model, already scoped."""
        return select(self.model).where(self.tenant_filter)

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """Return one record by primary key from this organization only.

        Deliberately not ``session.get``: that would fetch by primary key
        alone, and another tenant's row would come back. A row outside the
        organization is reported as absent, not as forbidden - whether an id
        exists elsewhere is not the caller's business.
        """
        result = await self.session.execute(
            self.select().where(self.model.id == entity_id)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[ModelT]:
        """Return a page of this organization's records."""
        statement = self.select().limit(limit).offset(offset)
        created_at = getattr(self.model, "created_at", None)
        if created_at is not None:
            statement = statement.order_by(created_at.desc())
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def count(self) -> int:
        """Count this organization's records."""
        result = await self.session.execute(
            select(func.count()).select_from(self.model).where(self.tenant_filter)
        )
        return result.scalar_one()

    def add(self, entity: ModelT) -> ModelT:
        """Stage a new record, stamping it with this organization.

        Set here rather than trusted from the caller, so a service cannot
        create a row owned by someone else even by mistake.
        """
        entity.organization_id = self.organization_id  # type: ignore[attr-defined]
        self.session.add(entity)
        return entity

    async def delete(self, entity_id: uuid.UUID) -> bool:
        """Delete one of this organization's records.

        Returns False for an id belonging to another tenant, exactly as for an
        id that does not exist.
        """
        entity = await self.get(entity_id)
        if entity is None:
            return False

        await self.session.delete(entity)
        return True
