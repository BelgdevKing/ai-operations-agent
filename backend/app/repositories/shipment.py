"""Shipment and charge data access."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import selectinload

from app.models.business import Shipment, ShipmentCharge
from app.repositories.tenant import TenantScopedRepository

# Ceiling on how many charges one shipment may contribute to an answer. Freight
# shipments carry a handful; anything near this is a data problem, and loading
# an unbounded set into a tool result is not the way to find out.
MAX_CHARGES = 200


class ShipmentRepository(TenantScopedRepository[Shipment]):
    """Shipments of one organization."""

    model = Shipment

    async def get_by_reference(self, reference: str) -> Shipment | None:
        """One shipment by the reference a customer quotes, e.g. ``ABC123``.

        Returns None when this organization has no such shipment - including
        when another organization does. The caller cannot tell those apart, and
        that is deliberate: a different answer would confirm the existence of
        another tenant's consignment to anyone who could guess a reference.
        """
        statement = self.select().where(Shipment.reference == reference)
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_with_customer(self, reference: str) -> Shipment | None:
        """The same lookup, with the customer loaded.

        Eager-loaded because async SQLAlchemy cannot lazy-load on attribute
        access, and every shipment answer names its customer.
        """
        statement = (
            self.select()
            .where(Shipment.reference == reference)
            .options(selectinload(Shipment.customer))
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int = 50
    ) -> Sequence[Shipment]:
        """Shipments belonging to one customer of this organization."""
        statement = (
            self.select()
            .where(Shipment.customer_id == customer_id)
            .order_by(Shipment.created_at.desc(), Shipment.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()


class ShipmentChargeRepository(TenantScopedRepository[ShipmentCharge]):
    """Charges of one organization."""

    model = ShipmentCharge

    async def list_for_shipment(
        self, shipment_id: uuid.UUID, *, limit: int = MAX_CHARGES
    ) -> Sequence[ShipmentCharge]:
        """Every charge against one shipment, oldest first.

        Scoped twice over: this repository filters on organization, and the
        shipment id could only have come from a shipment this organization owns.
        The composite foreign key means a charge from elsewhere could not have
        been attached to it in the first place.
        """
        statement = (
            self.select()
            .where(ShipmentCharge.shipment_id == shipment_id)
            .order_by(ShipmentCharge.created_at, ShipmentCharge.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()
