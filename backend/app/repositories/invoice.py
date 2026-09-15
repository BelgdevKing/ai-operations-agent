"""Invoice data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.models.business import Invoice
from app.repositories.tenant import TenantScopedRepository

# Invoices accumulate, so every listing here is bounded. A tool that returned a
# customer's whole billing history would grow a conversation without limit.
MAX_INVOICES = 50


class InvoiceRepository(TenantScopedRepository[Invoice]):
    """Invoices of one organization."""

    model = Invoice

    async def get_by_number(self, number: str) -> Invoice | None:
        """One invoice by its number, unique within this organization."""
        statement = self.select().where(Invoice.number == number)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int = MAX_INVOICES
    ) -> Sequence[Invoice]:
        """A customer's invoices, newest first."""
        statement = (
            self.select()
            .where(Invoice.customer_id == customer_id)
            .order_by(Invoice.created_at.desc(), Invoice.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def list_for_shipment(
        self, shipment_id: uuid.UUID, *, limit: int = MAX_INVOICES
    ) -> Sequence[Invoice]:
        """Invoices raised against one shipment.

        An empty list is an ordinary answer: an invoice may cover several
        shipments or none in particular, so ``shipment_id`` is nullable and a
        shipment can legitimately have none of its own.
        """
        statement = (
            self.select()
            .where(Invoice.shipment_id == shipment_id)
            .order_by(Invoice.created_at.desc(), Invoice.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
