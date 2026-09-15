"""Customer tool: who the account is, and how much is open for them."""

from __future__ import annotations

from app.models.enums import ShipmentStatus
from app.repositories.customer import CustomerRepository
from app.repositories.shipment import ShipmentRepository
from app.tools.business.base import BusinessTool, not_found
from app.tools.business.schemas import CustomerOutput, CustomerReferenceInput, CustomerSummary
from app.tools.models import ToolExecutionContext, ToolMetadata, ToolSafety

# A shipment is "open" until it has arrived or been called off.
CLOSED_STATUSES = frozenset({ShipmentStatus.DELIVERED, ShipmentStatus.CANCELLED})


class GetCustomerTool(BusinessTool[CustomerReferenceInput, CustomerOutput]):
    """A customer account, by the reference an operator would quote."""

    metadata = ToolMetadata(
        name="get_customer",
        description=(
            "Look up a customer by their account reference, for example "
            "CUST-1001. Returns their name, status, contact address and how many "
            "shipments are still in progress for them."
        ),
        safety=ToolSafety.READ_ONLY,
        timeout_seconds=10.0,
    )
    input_model = CustomerReferenceInput
    output_model = CustomerOutput

    async def execute(
        self, arguments: CustomerReferenceInput, context: ToolExecutionContext
    ) -> CustomerOutput:
        customers = CustomerRepository(self._session, context.organization_id)
        shipments = ShipmentRepository(self._session, context.organization_id)

        customer = await customers.get_by_reference(arguments.customer_reference)
        if customer is None:
            raise not_found("customer")

        # Bounded by the repository. The count is "open shipments among the most
        # recent", which is what an operator means by the question, and not a
        # reason to load a customer's entire history.
        recent = await shipments.list_for_customer(customer.id)
        open_shipments = sum(1 for shipment in recent if shipment.status not in CLOSED_STATUSES)

        return CustomerOutput(
            customer=CustomerSummary(
                reference=customer.reference,
                name=customer.name,
                status=customer.status.value,
            ),
            email=customer.email,
            open_shipments=open_shipments,
        )
