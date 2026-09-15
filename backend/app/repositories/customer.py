"""Customer data access.

Every query goes through :class:`TenantScopedRepository`, so the organization
predicate is applied by the base class rather than remembered by each method.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.business import Customer
from app.repositories.tenant import TenantScopedRepository


class CustomerRepository(TenantScopedRepository[Customer]):
    """Customers of one organization."""

    model = Customer

    async def get_by_reference(self, reference: str) -> Customer | None:
        """One customer by the identifier people quote.

        References are unique per organization, not globally: two tenants may
        each have a ``CUST-1001``, and this can only ever see its own.
        """
        statement = self.select().where(Customer.reference == reference)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_customers(self, *, limit: int = 50, offset: int = 0) -> Sequence[Customer]:
        """A page of customers, oldest first.

        Bounded by construction - there is no method here that returns every
        row, because a tool calling one would eventually load a tenant's entire
        book into memory.
        """
        statement = (
            self.select().order_by(Customer.created_at, Customer.id).limit(limit).offset(offset)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
