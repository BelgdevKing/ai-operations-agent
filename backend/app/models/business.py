"""Customers, shipments, charges and invoices.

The operational records an agent answers questions about. Four tables, all
tenant-owned, related to each other by **composite foreign keys that carry the
organization**::

    shipments        (customer_id, organization_id) -> customers(id, organization_id)
    shipment_charges (shipment_id, organization_id) -> shipments(id, organization_id)
    invoices         (customer_id, organization_id) -> customers(id, organization_id)
    invoices         (shipment_id, organization_id) -> shipments(id, organization_id)

That is the point of the design, and it is worth being explicit about. A plain
single-column foreign key would let application code attach one organization's
charge to another organization's shipment; the row would be accepted, and a
scoped query joining the two would then return it. With the tenant inside the
key, the database refuses the row in the first place - so cross-tenant
relationships are not merely untested, they are unrepresentable.

The redundant ``UNIQUE(id, organization_id)`` on the parents exists only to give
those composite keys something to reference.

Money is ``Numeric(12, 2)`` and arrives in Python as ``Decimal``. Never a float:
binary floating point cannot hold 0.10, and an invoice total that is out by a
penny is worse than one that fails loudly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    ChargeStatus,
    ChargeType,
    CustomerStatus,
    InvoiceStatus,
    ShipmentStatus,
    enum_column,
)
from app.models.mixins import (
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.organization import Organization

# Room for ten figures and exact pennies. Wider than freight needs, narrow
# enough that an absurd value is refused rather than stored.
MONEY = Numeric(12, 2)

# ISO 4217, upper case. A constraint rather than an enum: the list of world
# currencies is not ours to maintain, but the shape of the code is.
CURRENCY_LENGTH = 3
CURRENCY_PATTERN = "^[A-Z]{3}$"


# Every relationship below is ``viewonly``. The composite foreign keys share
# ``organization_id`` with each table's own organization key, so more than one
# relationship could claim to populate that column - which is exactly what
# SQLAlchemy warns about. Nothing here needs to write through a relationship:
# the tenant is set explicitly when a row is built, and deletes cascade in the
# database. Marking them read-only removes the ambiguity rather than silencing it.


def _currency_check(table: str) -> CheckConstraint:
    return CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name=f"{table}_currency_iso4217")


class Customer(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A company the organization does business with."""

    __tablename__ = "customers"

    # The identifier people actually quote. Unique per organization, not
    # globally: two tenants may both call an account CUST-1001.
    reference: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    status: Mapped[CustomerStatus] = mapped_column(
        enum_column(CustomerStatus, "status"),
        nullable=False,
        default=CustomerStatus.ACTIVE,
        server_default=CustomerStatus.ACTIVE.value,
    )

    organization: Mapped[Organization] = relationship(viewonly=True)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "reference", name="uq_customers_organization_reference"
        ),
        # Referenced by the composite foreign keys below. Redundant with the
        # primary key on its own, and the reason cross-tenant rows cannot exist.
        UniqueConstraint("id", "organization_id", name="uq_customers_id_organization_id"),
        Index("ix_customers_organization_id_status", "organization_id", "status"),
    )


class Shipment(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One consignment moving between two places."""

    __tablename__ = "shipments"

    reference: Mapped[str] = mapped_column(String(64), nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    status: Mapped[ShipmentStatus] = mapped_column(
        enum_column(ShipmentStatus, "status"),
        nullable=False,
        default=ShipmentStatus.PENDING,
        server_default=ShipmentStatus.PENDING.value,
    )

    carrier: Mapped[str | None] = mapped_column(String(100), nullable=True)
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
    destination: Mapped[str] = mapped_column(String(200), nullable=False)

    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    customer: Mapped[Customer] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="CASCADE",
            name="fk_shipments_customer_id_organization_id_customers",
        ),
        UniqueConstraint(
            "organization_id", "reference", name="uq_shipments_organization_reference"
        ),
        UniqueConstraint("id", "organization_id", name="uq_shipments_id_organization_id"),
        Index("ix_shipments_organization_id_status", "organization_id", "status"),
        # Supports the composite foreign key and "this customer's shipments".
        Index("ix_shipments_customer_id_organization_id", "customer_id", "organization_id"),
    )


class ShipmentCharge(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """One amount billed against a shipment.

    ``TimestampMixin`` rather than ``CreatedAtMixin``: a charge is updated when
    it is paid or waived, so it is not a write-once row.
    """

    __tablename__ = "shipment_charges"

    shipment_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    charge_type: Mapped[ChargeType] = mapped_column(
        enum_column(ChargeType, "charge_type"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH), nullable=False)

    status: Mapped[ChargeStatus] = mapped_column(
        enum_column(ChargeStatus, "status"),
        nullable=False,
        default=ChargeStatus.OUTSTANDING,
        server_default=ChargeStatus.OUTSTANDING.value,
    )

    organization: Mapped[Organization] = relationship(viewonly=True)
    shipment: Mapped[Shipment] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["shipment_id", "organization_id"],
            ["shipments.id", "shipments.organization_id"],
            ondelete="CASCADE",
            name="fk_shipment_charges_shipment_id_organization_id_shipments",
        ),
        _currency_check("shipment_charges"),
        CheckConstraint("amount >= 0", name="shipment_charges_amount_not_negative"),
        Index("ix_shipment_charges_shipment_id_organization_id", "shipment_id", "organization_id"),
        Index("ix_shipment_charges_organization_id_status", "organization_id", "status"),
    )


class Invoice(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """A bill issued to a customer, optionally for one shipment.

    ``amount_paid`` is stored rather than derived because part payments are
    ordinary, and "how much is still owed" has to be answerable without a
    payments ledger that does not exist yet. Whether the invoice is *overdue* is
    not stored: that depends on today.
    """

    __tablename__ = "invoices"

    number: Mapped[str] = mapped_column(String(64), nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Nullable: an invoice may cover several shipments or none in particular.
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    status: Mapped[InvoiceStatus] = mapped_column(
        enum_column(InvoiceStatus, "status"),
        nullable=False,
        default=InvoiceStatus.DRAFT,
        server_default=InvoiceStatus.DRAFT.value,
    )

    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    amount_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))

    currency: Mapped[str] = mapped_column(String(CURRENCY_LENGTH), nullable=False)

    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(viewonly=True)
    customer: Mapped[Customer] = relationship(viewonly=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="CASCADE",
            name="fk_invoices_customer_id_organization_id_customers",
        ),
        ForeignKeyConstraint(
            ["shipment_id", "organization_id"],
            ["shipments.id", "shipments.organization_id"],
            ondelete="CASCADE",
            name="fk_invoices_shipment_id_organization_id_shipments",
        ),
        UniqueConstraint("organization_id", "number", name="uq_invoices_organization_number"),
        _currency_check("invoices"),
        CheckConstraint("amount_paid >= 0", name="invoices_amount_paid_not_negative"),
        CheckConstraint("total >= 0", name="invoices_total_not_negative"),
        Index("ix_invoices_customer_id_organization_id", "customer_id", "organization_id"),
        Index("ix_invoices_shipment_id_organization_id", "shipment_id", "organization_id"),
        Index("ix_invoices_organization_id_status", "organization_id", "status"),
    )
