"""Writing the demo dataset into a database.

Idempotent by construction: every row's id is a `uuid5` of its business key, so
the loader can ask "is this already here?" and answer without guessing. Running
it twice updates the same rows rather than creating a second copy of the
dataset - which matters, because the interesting references are deliberately
duplicated across organizations and a careless re-run would make them ambiguous.

Reusable from a script and from tests, so what a developer sees locally is what
the tests exercise.

The update branches refresh the dates as well as the statuses. Dates in the
dataset are offsets from an anchor that moves with today, so a re-run is also
how a demo database seeded last month stops claiming its shipments are still
in transit - leaving them at their first-seeded values would age the data.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.dataset import (
    DEMO_ORGANIZATIONS,
    OrganizationSpec,
    demo_id,
)
from app.models.business import Customer, Invoice, Shipment, ShipmentCharge
from app.models.enums import CustomerStatus, MemberRole, MembershipStatus, OrganizationStatus
from app.models.organization import Organization, OrganizationMember
from app.models.user import User

logger = logging.getLogger(__name__)


async def seed_demo_data(
    session: AsyncSession,
    *,
    specs: Sequence[OrganizationSpec] = DEMO_ORGANIZATIONS,
    attach_user_email: str | None = None,
) -> dict[str, uuid.UUID]:
    """Create or refresh the demo records.

    Args:
        session: Written to but not committed - the caller owns the transaction,
            which is what lets a test roll the whole thing back.
        specs: The organizations to write. Defaults to the full dataset.
        attach_user_email: An existing account to make an owner of each demo
            organization, so a developer who has registered can actually see
            the data. No password is involved and none is stored here.

    Returns:
        Organization slug to id, so a caller can find what it just wrote.
    """
    created: dict[str, uuid.UUID] = {}

    for spec in specs:
        organization = await _upsert_organization(session, spec)
        created[spec.slug] = organization.id

        customers = await _upsert_customers(session, spec, organization.id)
        shipments = await _upsert_shipments(session, spec, organization.id, customers)
        await _upsert_charges(session, spec, organization.id, shipments)
        await _upsert_invoices(session, spec, organization.id, customers, shipments)

        if attach_user_email:
            await _attach_owner(session, organization.id, attach_user_email)

    await session.flush()

    logger.info(
        "Demo data seeded",
        extra={"context": {"organizations": sorted(created), "count": len(created)}},
    )
    return created


# -- One kind of row at a time ------------------------------------------------


async def _upsert_organization(session: AsyncSession, spec: OrganizationSpec) -> Organization:
    """Find the demo organization by slug, or create it.

    By slug rather than by generated id: a developer may already have an
    organization on that slug, and taking it over is better than failing on the
    unique constraint.
    """
    existing = (
        await session.execute(select(Organization).where(Organization.slug == spec.slug))
    ).scalar_one_or_none()

    if existing is not None:
        existing.name = spec.name
        existing.status = OrganizationStatus.ACTIVE
        return existing

    organization = Organization(
        id=demo_id("organization", spec.slug),
        name=spec.name,
        slug=spec.slug,
        status=OrganizationStatus.ACTIVE,
    )
    session.add(organization)
    await session.flush()
    return organization


async def _upsert_customers(
    session: AsyncSession, spec: OrganizationSpec, organization_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}

    for customer_spec in spec.customers:
        customer_id = demo_id("customer", spec.slug, customer_spec.reference)
        ids[customer_spec.reference] = customer_id

        existing = await session.get(Customer, customer_id)
        if existing is None:
            session.add(
                Customer(
                    id=customer_id,
                    organization_id=organization_id,
                    reference=customer_spec.reference,
                    name=customer_spec.name,
                    email=customer_spec.email,
                    status=CustomerStatus.ACTIVE,
                )
            )
        else:
            existing.name = customer_spec.name
            existing.email = customer_spec.email

    await session.flush()
    return ids


async def _upsert_shipments(
    session: AsyncSession,
    spec: OrganizationSpec,
    organization_id: uuid.UUID,
    customers: dict[str, uuid.UUID],
) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}

    for shipment_spec in spec.shipments:
        shipment_id = demo_id("shipment", spec.slug, shipment_spec.reference)
        ids[shipment_spec.reference] = shipment_id

        existing = await session.get(Shipment, shipment_id)
        if existing is None:
            session.add(
                Shipment(
                    id=shipment_id,
                    organization_id=organization_id,
                    reference=shipment_spec.reference,
                    customer_id=customers[shipment_spec.customer_reference],
                    status=shipment_spec.status,
                    carrier=shipment_spec.carrier,
                    origin=shipment_spec.origin,
                    destination=shipment_spec.destination,
                    shipped_at=shipment_spec.shipped_at,
                    estimated_delivery_at=shipment_spec.estimated_delivery_at,
                    delivered_at=shipment_spec.delivered_at,
                )
            )
        else:
            existing.status = shipment_spec.status
            existing.shipped_at = shipment_spec.shipped_at
            existing.estimated_delivery_at = shipment_spec.estimated_delivery_at
            existing.delivered_at = shipment_spec.delivered_at

    await session.flush()
    return ids


async def _upsert_charges(
    session: AsyncSession,
    spec: OrganizationSpec,
    organization_id: uuid.UUID,
    shipments: dict[str, uuid.UUID],
) -> None:
    for shipment_spec in spec.shipments:
        for charge_spec in shipment_spec.charges:
            charge_id = demo_id("charge", spec.slug, shipment_spec.reference, charge_spec.key)

            existing = await session.get(ShipmentCharge, charge_id)
            if existing is None:
                session.add(
                    ShipmentCharge(
                        id=charge_id,
                        organization_id=organization_id,
                        shipment_id=shipments[shipment_spec.reference],
                        charge_type=charge_spec.charge_type,
                        description=charge_spec.description,
                        amount=charge_spec.amount,
                        currency=spec.currency,
                        status=charge_spec.status,
                    )
                )
            else:
                existing.amount = charge_spec.amount
                existing.status = charge_spec.status

    await session.flush()


async def _upsert_invoices(
    session: AsyncSession,
    spec: OrganizationSpec,
    organization_id: uuid.UUID,
    customers: dict[str, uuid.UUID],
    shipments: dict[str, uuid.UUID],
) -> None:
    for invoice_spec in spec.invoices:
        invoice_id = demo_id("invoice", spec.slug, invoice_spec.number)

        shipment_id = (
            shipments[invoice_spec.shipment_reference] if invoice_spec.shipment_reference else None
        )

        existing = await session.get(Invoice, invoice_id)
        if existing is None:
            session.add(
                Invoice(
                    id=invoice_id,
                    organization_id=organization_id,
                    number=invoice_spec.number,
                    customer_id=customers[invoice_spec.customer_reference],
                    shipment_id=shipment_id,
                    status=invoice_spec.status,
                    subtotal=invoice_spec.subtotal,
                    tax=invoice_spec.tax,
                    total=invoice_spec.total,
                    amount_paid=invoice_spec.amount_paid,
                    currency=spec.currency,
                    due_date=invoice_spec.due_date,
                    paid_at=invoice_spec.paid_at,
                )
            )
        else:
            existing.status = invoice_spec.status
            existing.amount_paid = invoice_spec.amount_paid
            existing.due_date = invoice_spec.due_date
            existing.paid_at = invoice_spec.paid_at

    await session.flush()


async def _attach_owner(session: AsyncSession, organization_id: uuid.UUID, email: str) -> None:
    """Make an existing account an owner of a demo organization.

    Silent when the address is unknown: seeding business data should not fail
    because a convenience flag named somebody who has not registered.
    """
    user = (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()

    if user is None:
        logger.warning("No account to attach", extra={"context": {"email_known": False}})
        return

    existing = (
        await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.status = MembershipStatus.ACTIVE
        return

    session.add(
        OrganizationMember(
            organization_id=organization_id,
            user_id=user.id,
            role=MemberRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()
