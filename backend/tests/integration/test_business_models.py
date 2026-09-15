"""The business tables themselves: what the database will and will not accept.

Constraint tests, not query tests. Each one asserts a rule the schema enforces,
so that a later migration relaxing it fails here rather than in production.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Customer, Invoice, Shipment, ShipmentCharge
from app.models.enums import ChargeType, OrganizationStatus, ShipmentStatus
from app.models.organization import Organization

pytestmark = pytest.mark.integration


async def an_organization(session: AsyncSession, slug: str) -> Organization:
    organization = Organization(name=f"Org {slug}", slug=slug, status=OrganizationStatus.ACTIVE)
    session.add(organization)
    await session.flush()
    return organization


async def a_customer(session: AsyncSession, organization: Organization, reference: str) -> Customer:
    customer = Customer(
        organization_id=organization.id, reference=reference, name=f"Customer {reference}"
    )
    session.add(customer)
    await session.flush()
    return customer


# -- Creation -----------------------------------------------------------------


async def test_a_customer_can_be_created(session: AsyncSession) -> None:
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")

    customer = await a_customer(session, organization, "CUST-1")

    assert customer.id is not None
    assert customer.created_at is not None
    assert customer.status.value == "active", "defaults applied by the database"


async def test_a_shipment_needs_an_origin_and_destination(session: AsyncSession) -> None:
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    customer = await a_customer(session, organization, "CUST-1")

    session.add(
        Shipment(
            organization_id=organization.id,
            reference="S-1",
            customer_id=customer.id,
            origin="London, GB",
            destination=None,  # type: ignore[arg-type]
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_two_organizations_may_use_the_same_reference(session: AsyncSession) -> None:
    """Unique per tenant, not globally - which is what makes the isolation
    tests elsewhere meaningful."""
    first = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    second = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")

    await a_customer(session, first, "CUST-1001")
    await a_customer(session, second, "CUST-1001")

    found = (
        (
            await session.execute(
                select(Customer).where(
                    Customer.reference == "CUST-1001",
                    Customer.organization_id.in_([first.id, second.id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(found) == 2
    assert {customer.organization_id for customer in found} == {first.id, second.id}


async def test_one_organization_may_not_reuse_a_reference(session: AsyncSession) -> None:
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    await a_customer(session, organization, "CUST-1001")

    session.add(Customer(organization_id=organization.id, reference="CUST-1001", name="Duplicate"))

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# -- Money --------------------------------------------------------------------


async def test_money_survives_the_round_trip_exactly(session: AsyncSession) -> None:
    """Numeric(12, 2), so 180.50 comes back as 180.50 and as a Decimal."""
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    customer = await a_customer(session, organization, "CUST-1")

    shipment = Shipment(
        organization_id=organization.id,
        reference="S-1",
        customer_id=customer.id,
        origin="A",
        destination="B",
        status=ShipmentStatus.IN_TRANSIT,
    )
    session.add(shipment)
    await session.flush()

    session.add(
        ShipmentCharge(
            organization_id=organization.id,
            shipment_id=shipment.id,
            charge_type=ChargeType.FUEL_SURCHARGE,
            amount=Decimal("180.50"),
            currency="GBP",
        )
    )
    await session.flush()

    # Captured before expiring: reading an expired attribute would trigger a
    # refresh, and that is database IO in the wrong place.
    shipment_id = shipment.id
    session.expire_all()

    stored = (
        await session.execute(
            select(ShipmentCharge).where(ShipmentCharge.shipment_id == shipment_id)
        )
    ).scalar_one()

    assert isinstance(stored.amount, Decimal)
    assert stored.amount == Decimal("180.50")
    assert str(stored.amount) == "180.50"


async def test_a_negative_charge_is_refused(session: AsyncSession) -> None:
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    customer = await a_customer(session, organization, "CUST-1")

    shipment = Shipment(
        organization_id=organization.id,
        reference="S-1",
        customer_id=customer.id,
        origin="A",
        destination="B",
    )
    session.add(shipment)
    await session.flush()

    session.add(
        ShipmentCharge(
            organization_id=organization.id,
            shipment_id=shipment.id,
            charge_type=ChargeType.FREIGHT,
            amount=Decimal("-10.00"),
            currency="GBP",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_a_currency_must_look_like_an_iso_code(session: AsyncSession) -> None:
    organization = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    customer = await a_customer(session, organization, "CUST-1")

    session.add(
        Invoice(
            organization_id=organization.id,
            number="INV-1",
            customer_id=customer.id,
            subtotal=Decimal("10.00"),
            tax=Decimal("0.00"),
            total=Decimal("10.00"),
            amount_paid=Decimal("0.00"),
            currency="pounds",  # not ISO 4217
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await session.flush()
    await session.rollback()


# -- Relationships carry the tenant -------------------------------------------


async def test_a_shipment_cannot_reference_another_organizations_customer(
    session: AsyncSession,
) -> None:
    """The composite foreign key. Without it this row would be accepted, and a
    scoped join would then return one tenant's customer to another."""
    first = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    second = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    theirs = await a_customer(session, second, "CUST-1")

    session.add(
        Shipment(
            organization_id=first.id,
            reference="S-1",
            customer_id=theirs.id,
            origin="A",
            destination="B",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_an_invoice_cannot_reference_another_organizations_shipment(
    session: AsyncSession,
) -> None:
    first = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")
    second = await an_organization(session, f"org-{uuid.uuid4().hex[:8]}")

    ours = await a_customer(session, first, "CUST-1")
    theirs_customer = await a_customer(session, second, "CUST-1")

    theirs_shipment = Shipment(
        organization_id=second.id,
        reference="S-1",
        customer_id=theirs_customer.id,
        origin="A",
        destination="B",
    )
    session.add(theirs_shipment)
    await session.flush()

    session.add(
        Invoice(
            organization_id=first.id,
            number="INV-1",
            customer_id=ours.id,
            shipment_id=theirs_shipment.id,
            subtotal=Decimal("10.00"),
            tax=Decimal("0.00"),
            total=Decimal("10.00"),
            amount_paid=Decimal("0.00"),
            currency="GBP",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
