"""The demo dataset, as data.

Kept separate from the script that writes it so tests can use exactly what a
developer sees locally, rather than a second set of fixtures that drifts from it.

Everything here is obviously synthetic: invented company names, invented
addresses, no real person, no real contact detail. The two organizations
deliberately share a customer reference (``CUST-1001``) and a shipment
reference (``ABC123``) with entirely different records behind them - which is
what makes tenant isolation visible rather than theoretical.

Identifiers are derived with ``uuid5`` from a fixed namespace, so running the
seed twice produces the same rows rather than a second copy of everything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.models.enums import ChargeStatus, ChargeType, InvoiceStatus, ShipmentStatus

# A fixed namespace, so every id below is a pure function of its key.
NAMESPACE = uuid.UUID("7c3f0b9e-5a1d-4f86-9c2a-1d0e5b7a4c31")

# Anchored to today rather than to a fixed calendar date. Every date below is
# an offset from this, and "overdue" is derived from the real clock at read
# time (app/tools/business/money.py), so a fixed anchor does not make the data
# stable - it makes it expire: the invoice seeded as "due in a fortnight" comes
# due, and stays overdue from then on. Relative offsets keep meaning what they
# say, and a freshly seeded demo keeps showing shipments that have not arrived
# yet. Normalised to noon UTC so the dates are stable within a run and do not
# turn over halfway through one.
ANCHOR = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def demo_id(*parts: str) -> uuid.UUID:
    """A stable id for a seeded row."""
    return uuid.uuid5(NAMESPACE, ":".join(parts))


@dataclass(frozen=True)
class ChargeSpec:
    key: str
    charge_type: ChargeType
    description: str
    amount: Decimal
    status: ChargeStatus


@dataclass(frozen=True)
class ShipmentSpec:
    reference: str
    customer_reference: str
    status: ShipmentStatus
    carrier: str
    origin: str
    destination: str
    shipped_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    delivered_at: datetime | None = None
    charges: tuple[ChargeSpec, ...] = ()


@dataclass(frozen=True)
class InvoiceSpec:
    number: str
    customer_reference: str
    shipment_reference: str | None
    status: InvoiceStatus
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    amount_paid: Decimal
    due_date: date | None = None
    paid_at: datetime | None = None


@dataclass(frozen=True)
class CustomerSpec:
    reference: str
    name: str
    email: str


@dataclass(frozen=True)
class OrganizationSpec:
    slug: str
    name: str
    currency: str
    customers: tuple[CustomerSpec, ...] = field(default_factory=tuple)
    shipments: tuple[ShipmentSpec, ...] = field(default_factory=tuple)
    invoices: tuple[InvoiceSpec, ...] = field(default_factory=tuple)


def _money(value: str) -> Decimal:
    return Decimal(value)


# -- Organization one: the one the demo scenario is about ---------------------

NORTHWIND = OrganizationSpec(
    slug="demo-northwind",
    name="Northwind Freight (demo)",
    currency="GBP",
    customers=(
        CustomerSpec("CUST-1001", "Acme Industries", "operations@acme.example"),
        CustomerSpec("CUST-1002", "Bluebird Retail", "logistics@bluebird.example"),
        CustomerSpec("CUST-1003", "Cedar Foods", "supply@cedarfoods.example"),
    ),
    shipments=(
        # The shipment the demo question asks about: in transit, money owed.
        ShipmentSpec(
            reference="ABC123",
            customer_reference="CUST-1001",
            status=ShipmentStatus.IN_TRANSIT,
            carrier="Meridian Lines",
            origin="London, GB",
            destination="Hamburg, DE",
            shipped_at=ANCHOR - timedelta(days=4),
            estimated_delivery_at=ANCHOR + timedelta(days=2),
            charges=(
                ChargeSpec(
                    "freight",
                    ChargeType.FREIGHT,
                    "Ocean freight",
                    _money("1250.00"),
                    ChargeStatus.OUTSTANDING,
                ),
                ChargeSpec(
                    "fuel",
                    ChargeType.FUEL_SURCHARGE,
                    "Fuel surcharge",
                    _money("180.50"),
                    ChargeStatus.OUTSTANDING,
                ),
                ChargeSpec(
                    "handling",
                    ChargeType.HANDLING,
                    "Terminal handling",
                    _money("75.00"),
                    ChargeStatus.PAID,
                ),
            ),
        ),
        # Delivered, everything settled: the "no outstanding charges" case.
        ShipmentSpec(
            reference="DEF456",
            customer_reference="CUST-1002",
            status=ShipmentStatus.DELIVERED,
            carrier="Kestrel Road",
            origin="Leeds, GB",
            destination="Dublin, IE",
            shipped_at=ANCHOR - timedelta(days=14),
            estimated_delivery_at=ANCHOR - timedelta(days=9),
            delivered_at=ANCHOR - timedelta(days=10),
            charges=(
                ChargeSpec(
                    "freight",
                    ChargeType.FREIGHT,
                    "Road freight",
                    _money("640.00"),
                    ChargeStatus.PAID,
                ),
            ),
        ),
        # Nothing billed yet: an empty charge list is a normal answer.
        ShipmentSpec(
            reference="GHI789",
            customer_reference="CUST-1003",
            status=ShipmentStatus.PENDING,
            carrier="Meridian Lines",
            origin="Bristol, GB",
            destination="Lisbon, PT",
            estimated_delivery_at=ANCHOR + timedelta(days=9),
        ),
        # Held up, and accruing storage.
        ShipmentSpec(
            reference="JKL012",
            customer_reference="CUST-1001",
            status=ShipmentStatus.EXCEPTION,
            carrier="Kestrel Road",
            origin="Dover, GB",
            destination="Calais, FR",
            shipped_at=ANCHOR - timedelta(days=6),
            estimated_delivery_at=ANCHOR - timedelta(days=3),
            charges=(
                ChargeSpec(
                    "storage",
                    ChargeType.STORAGE,
                    "Customs hold storage",
                    _money("95.00"),
                    ChargeStatus.OUTSTANDING,
                ),
                ChargeSpec(
                    "customs",
                    ChargeType.CUSTOMS_DUTY,
                    "Import duty",
                    _money("210.25"),
                    ChargeStatus.OUTSTANDING,
                ),
            ),
        ),
    ),
    invoices=(
        # Unpaid and past due: overdue is derived, never stored.
        InvoiceSpec(
            number="INV-2026-0001",
            customer_reference="CUST-1001",
            shipment_reference="ABC123",
            status=InvoiceStatus.ISSUED,
            subtotal=_money("1430.50"),
            tax=_money("286.10"),
            total=_money("1716.60"),
            amount_paid=_money("0.00"),
            due_date=(ANCHOR - timedelta(days=7)).date(),
        ),
        # Settled in full.
        InvoiceSpec(
            number="INV-2026-0002",
            customer_reference="CUST-1002",
            shipment_reference="DEF456",
            status=InvoiceStatus.PAID,
            subtotal=_money("640.00"),
            tax=_money("128.00"),
            total=_money("768.00"),
            amount_paid=_money("768.00"),
            due_date=(ANCHOR - timedelta(days=20)).date(),
            paid_at=ANCHOR - timedelta(days=22),
        ),
        # Part paid, and not yet due: outstanding without being overdue.
        InvoiceSpec(
            number="INV-2026-0003",
            customer_reference="CUST-1001",
            shipment_reference=None,
            status=InvoiceStatus.PARTIALLY_PAID,
            subtotal=_money("500.00"),
            tax=_money("100.00"),
            total=_money("600.00"),
            amount_paid=_money("200.00"),
            due_date=(ANCHOR + timedelta(days=14)).date(),
        ),
    ),
)

# -- Organization two: same references, entirely different records ------------

GLOBEX = OrganizationSpec(
    slug="demo-globex",
    name="Globex Logistics (demo)",
    currency="USD",
    customers=(
        # Same reference as Northwind's first customer, on purpose.
        CustomerSpec("CUST-1001", "Initech Supplies", "ops@initech.example"),
        CustomerSpec("CUST-2002", "Umbrella Corp", "freight@umbrella.example"),
    ),
    shipments=(
        # Same reference as Northwind's ABC123, and nothing else in common.
        ShipmentSpec(
            reference="ABC123",
            customer_reference="CUST-1001",
            status=ShipmentStatus.DELIVERED,
            carrier="Pioneer Air",
            origin="Chicago, US",
            destination="Denver, US",
            shipped_at=ANCHOR - timedelta(days=20),
            estimated_delivery_at=ANCHOR - timedelta(days=17),
            delivered_at=ANCHOR - timedelta(days=17),
            charges=(
                ChargeSpec(
                    "freight",
                    ChargeType.FREIGHT,
                    "Air freight",
                    _money("900.00"),
                    ChargeStatus.PAID,
                ),
            ),
        ),
        ShipmentSpec(
            reference="XYZ999",
            customer_reference="CUST-2002",
            status=ShipmentStatus.IN_TRANSIT,
            carrier="Pioneer Air",
            origin="Seattle, US",
            destination="Phoenix, US",
            shipped_at=ANCHOR - timedelta(days=2),
            estimated_delivery_at=ANCHOR + timedelta(days=1),
            charges=(
                ChargeSpec(
                    "freight",
                    ChargeType.FREIGHT,
                    "Air freight",
                    _money("300.00"),
                    ChargeStatus.OUTSTANDING,
                ),
            ),
        ),
    ),
    invoices=(
        # Same number as Northwind's first invoice, different organization.
        InvoiceSpec(
            number="INV-2026-0001",
            customer_reference="CUST-1001",
            shipment_reference="ABC123",
            status=InvoiceStatus.PAID,
            subtotal=_money("900.00"),
            tax=_money("81.00"),
            total=_money("981.00"),
            amount_paid=_money("981.00"),
            due_date=(ANCHOR - timedelta(days=10)).date(),
            paid_at=ANCHOR - timedelta(days=12),
        ),
    ),
)

DEMO_ORGANIZATIONS: tuple[OrganizationSpec, ...] = (NORTHWIND, GLOBEX)
