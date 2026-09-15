"""What the business tools accept and return.

Explicit schemas, never a serialised ORM object. Two reasons, and both matter:
a model gets only the fields an operator would actually quote, and a column
added to a table later cannot silently start appearing in an answer.

Absent by design from every output below: primary keys, `organization_id`,
audit timestamps, and anything a customer record might accumulate that has no
bearing on an operational question.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ChargeStatus, ChargeType, InvoiceStatus, ShipmentStatus

# References are quoted by people and typed by a model, so they are bounded and
# stripped. Long enough for any real identifier, short enough that a paragraph
# of prose is refused before it reaches a query.
REFERENCE_MAX_LENGTH = 64


class BusinessInput(BaseModel):
    """Base for tool inputs.

    ``extra="forbid"`` is what makes an unexpected argument - including an
    attempt at ``organization_id`` - a refusal rather than something quietly
    dropped. The framework rejects reserved names before this, so this is the
    second of two independent defences.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ShipmentReferenceInput(BusinessInput):
    shipment_reference: str = Field(
        min_length=1,
        max_length=REFERENCE_MAX_LENGTH,
        description="The shipment reference a customer quotes, for example ABC123.",
    )


class CancelShipmentInput(BusinessInput):
    """Ask for a shipment to be cancelled.

    A reason is required and bounded. It is the sentence an approver reads in
    the conversation when deciding, and the one the record keeps afterwards -
    "the agent asked to cancel ABC123" is not a decision anybody can make well.
    """

    shipment_reference: str = Field(
        min_length=1,
        max_length=REFERENCE_MAX_LENGTH,
        description="The shipment to cancel, for example ABC123.",
    )
    reason: str = Field(
        min_length=3,
        max_length=500,
        description="Why the shipment should be cancelled, in the user's own terms.",
    )


class CustomerReferenceInput(BusinessInput):
    customer_reference: str = Field(
        min_length=1,
        max_length=REFERENCE_MAX_LENGTH,
        description="The customer account reference, for example CUST-1001.",
    )


class InvoiceLookupInput(BusinessInput):
    """Either a shipment or a customer, never both and never neither.

    A narrow business query rather than arbitrary filtering: the caller says
    which thing they are asking about, and the tool decides how to find it.
    """

    shipment_reference: str | None = Field(
        default=None,
        max_length=REFERENCE_MAX_LENGTH,
        description="Invoices raised against this shipment.",
    )
    customer_reference: str | None = Field(
        default=None,
        max_length=REFERENCE_MAX_LENGTH,
        description="Invoices raised for this customer. Ignored if a shipment is given.",
    )


# -- Outputs ------------------------------------------------------------------


class CustomerSummary(BaseModel):
    """A customer, as much of one as an operational answer needs."""

    reference: str
    name: str
    status: str


class ShipmentSummary(BaseModel):
    """A shipment's operational state."""

    shipment_reference: str
    status: ShipmentStatus
    carrier: str | None = None
    origin: str
    destination: str
    shipped_at: datetime | None = None
    estimated_delivery_at: datetime | None = None
    delivered_at: datetime | None = None
    customer: CustomerSummary


class ShipmentOutput(BaseModel):
    shipment: ShipmentSummary


class CancelShipmentOutput(BaseModel):
    """What was actually done.

    ``cancelled`` is stated rather than implied. A tool that returns a shipment
    and lets the reader infer the outcome is a tool whose failure looks like a
    success.
    """

    shipment_reference: str
    cancelled: bool
    status: ShipmentStatus
    previous_status: ShipmentStatus


class CustomerOutput(BaseModel):
    """A customer and how much work is open for them."""

    customer: CustomerSummary
    email: str | None = Field(
        default=None,
        description="Business contact address. The only personal detail returned, "
        "because an operator answering a query needs somewhere to reply.",
    )
    open_shipments: int = Field(description="Shipments not yet delivered or cancelled.")


class ChargeSummary(BaseModel):
    charge_type: ChargeType
    description: str | None = None
    amount: Decimal
    currency: str
    status: ChargeStatus
    outstanding: bool = Field(description="Whether this charge is still owed.")


class ShipmentChargesOutput(BaseModel):
    """Charges against a shipment, and what is still owed.

    The total is computed here rather than by the model. `currency` is null when
    nothing is outstanding - there is no currency to name for zero.
    """

    shipment_reference: str
    charges: list[ChargeSummary]
    total_outstanding: Decimal
    currency: str | None = None


class InvoiceSummary(BaseModel):
    number: str
    status: InvoiceStatus
    total: Decimal
    amount_paid: Decimal
    outstanding: Decimal
    currency: str
    due_date: date | None = None
    overdue: bool = Field(description="Past its due date with money still owed, as of today.")
    shipment_reference: str | None = None


class InvoicesOutput(BaseModel):
    invoices: list[InvoiceSummary]
    total_outstanding: Decimal
    currency: str | None = None
