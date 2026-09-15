"""Shipment tools: where is it, what is owed on it, and cancelling it.

Two of the three are read-only lookups. The third, ``cancel_shipment``, is the
platform's first tool that changes anything - and it is classified
``DESTRUCTIVE``, which the framework refuses to run without a person having
agreed to it. Nothing in this file enforces that: the classification does, in
``ToolMetadata``, and the executor holds the line.
"""

from __future__ import annotations

import logging

from app.models.enums import ShipmentStatus
from app.repositories.shipment import ShipmentChargeRepository, ShipmentRepository
from app.tools.business.base import BusinessTool, not_found
from app.tools.business.money import is_outstanding, total_outstanding
from app.tools.business.schemas import (
    CancelShipmentInput,
    CancelShipmentOutput,
    ChargeSummary,
    CustomerSummary,
    ShipmentChargesOutput,
    ShipmentOutput,
    ShipmentReferenceInput,
    ShipmentSummary,
)
from app.tools.exceptions import ToolError
from app.tools.models import ToolExecutionContext, ToolMetadata, ToolSafety

logger = logging.getLogger(__name__)

# What can still be stopped. A shipment that has already arrived, or has
# already been cancelled, is not one anybody can cancel - everything else is.
#
# ``EXCEPTION`` belongs here and its absence was a bug: a consignment that has
# gone wrong is the *most* likely thing somebody wants to cancel, and refusing
# it left the one operational case the tool exists for unreachable.
CANCELLABLE = frozenset(
    {ShipmentStatus.PENDING, ShipmentStatus.IN_TRANSIT, ShipmentStatus.EXCEPTION}
)


class GetShipmentTool(BusinessTool[ShipmentReferenceInput, ShipmentOutput]):
    """Where a shipment is."""

    metadata = ToolMetadata(
        name="get_shipment",
        description=(
            "Look up a shipment by its reference, for example ABC123. Returns "
            "its current status, route, carrier and expected delivery, with the "
            "customer it belongs to."
        ),
        safety=ToolSafety.READ_ONLY,
        timeout_seconds=10.0,
    )
    input_model = ShipmentReferenceInput
    output_model = ShipmentOutput

    async def execute(
        self, arguments: ShipmentReferenceInput, context: ToolExecutionContext
    ) -> ShipmentOutput:
        # The repository is built here, from the organization on the trusted
        # context. Nothing in `arguments` reaches this line.
        shipments = ShipmentRepository(self._session, context.organization_id)

        shipment = await shipments.get_with_customer(arguments.shipment_reference)
        if shipment is None:
            raise not_found("shipment")

        return ShipmentOutput(
            shipment=ShipmentSummary(
                shipment_reference=shipment.reference,
                status=shipment.status,
                carrier=shipment.carrier,
                origin=shipment.origin,
                destination=shipment.destination,
                shipped_at=shipment.shipped_at,
                estimated_delivery_at=shipment.estimated_delivery_at,
                delivered_at=shipment.delivered_at,
                customer=CustomerSummary(
                    reference=shipment.customer.reference,
                    name=shipment.customer.name,
                    status=shipment.customer.status.value,
                ),
            )
        )


class GetShipmentChargesTool(BusinessTool[ShipmentReferenceInput, ShipmentChargesOutput]):
    """What is billed against a shipment, and how much of it is still owed."""

    metadata = ToolMetadata(
        name="get_shipment_charges",
        description=(
            "List the charges raised against a shipment and total what is still "
            "outstanding. Use after finding a shipment to answer questions about "
            "money owed on it."
        ),
        safety=ToolSafety.READ_ONLY,
        timeout_seconds=10.0,
    )
    input_model = ShipmentReferenceInput
    output_model = ShipmentChargesOutput

    async def execute(
        self, arguments: ShipmentReferenceInput, context: ToolExecutionContext
    ) -> ShipmentChargesOutput:
        shipments = ShipmentRepository(self._session, context.organization_id)
        charges_repo = ShipmentChargeRepository(self._session, context.organization_id)

        shipment = await shipments.get_by_reference(arguments.shipment_reference)
        if shipment is None:
            raise not_found("shipment")

        charges = await charges_repo.list_for_shipment(shipment.id)

        # A shipment with nothing billed is an ordinary answer, not a failure:
        # an empty list and a zero total.
        total, currency = total_outstanding(charges)

        return ShipmentChargesOutput(
            shipment_reference=shipment.reference,
            charges=[
                ChargeSummary(
                    charge_type=charge.charge_type,
                    description=charge.description,
                    amount=charge.amount,
                    currency=charge.currency,
                    status=charge.status,
                    outstanding=is_outstanding(charge),
                )
                for charge in charges
            ],
            total_outstanding=total,
            currency=currency,
        )


class ShipmentNotCancellableError(ToolError):
    """The shipment exists but is past the point of being cancelled.

    A 409 rather than a failure: nothing is broken, the answer is simply no. The
    message names the state so the agent can tell the user why, and names
    nothing else.
    """

    status_code = 409
    code = "shipment_not_cancellable"
    message = "That shipment can no longer be cancelled."


class CancelShipmentTool(BusinessTool[CancelShipmentInput, CancelShipmentOutput]):
    """Cancel a shipment.

    **Destructive, and therefore gated.** ``ToolMetadata`` refuses to be built
    for a destructive tool that does not require approval, and the executor
    refuses to reach this body without a decision a person recorded. The first
    time an agent asks for this, the run pauses; this code runs only after
    somebody with the admin role has said yes, and at most once for that
    execution.

    The tenant comes from the context, as it does for every other business tool,
    so an approved cancellation can only ever land on a shipment belonging to the
    organization the run was for.
    """

    metadata = ToolMetadata(
        name="cancel_shipment",
        description=(
            "Cancel a shipment that has not yet been delivered. Requires a "
            "reason. This changes the shipment and cannot be undone, so a person "
            "has to approve it before it happens - ask only when the user has "
            "clearly asked for the shipment to be cancelled."
        ),
        safety=ToolSafety.DESTRUCTIVE,
        requires_approval=True,
        timeout_seconds=10.0,
    )
    input_model = CancelShipmentInput
    output_model = CancelShipmentOutput

    async def execute(
        self, arguments: CancelShipmentInput, context: ToolExecutionContext
    ) -> CancelShipmentOutput:
        shipments = ShipmentRepository(self._session, context.organization_id)

        shipment = await shipments.get_by_reference(arguments.shipment_reference)
        if shipment is None:
            raise not_found("shipment")

        previous = shipment.status
        if previous not in CANCELLABLE:
            raise ShipmentNotCancellableError(
                f"That shipment is {previous.value} and can no longer be cancelled.",
            )

        shipment.status = ShipmentStatus.CANCELLED
        await self._session.flush()

        # The reason is the user's own words about their own shipment, so it
        # stays in the conversation where the rest of the exchange is. What goes
        # to the log is that a cancellation happened, and under which execution.
        logger.info(
            "Shipment cancelled by an approved agent action",
            extra={
                "context": {
                    "tool_execution_id": str(context.tool_execution_id),
                    "run_id": str(context.run_id) if context.run_id else None,
                    "organization_id": str(context.organization_id),
                    "previous_status": previous.value,
                }
            },
        )

        return CancelShipmentOutput(
            shipment_reference=shipment.reference,
            cancelled=True,
            status=ShipmentStatus.CANCELLED,
            previous_status=previous,
        )
