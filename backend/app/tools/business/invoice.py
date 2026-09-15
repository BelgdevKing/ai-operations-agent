"""Invoice tool: what has been billed, and what is still owed."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from app.models.business import Invoice, Shipment
from app.repositories.customer import CustomerRepository
from app.repositories.invoice import InvoiceRepository
from app.repositories.shipment import ShipmentRepository
from app.tools.business.base import BusinessTool, not_found
from app.tools.business.money import ZERO, MixedCurrencyError, invoice_outstanding, is_overdue
from app.tools.business.schemas import InvoiceLookupInput, InvoicesOutput, InvoiceSummary
from app.tools.exceptions import ToolValidationError
from app.tools.models import ToolExecutionContext, ToolMetadata, ToolSafety


class GetInvoicesTool(BusinessTool[InvoiceLookupInput, InvoicesOutput]):
    """Invoices for one shipment or one customer."""

    metadata = ToolMetadata(
        name="get_invoices",
        description=(
            "List invoices for a shipment or for a customer, with what is still "
            "outstanding on each. Give either a shipment reference or a customer "
            "reference, not both."
        ),
        safety=ToolSafety.READ_ONLY,
        timeout_seconds=10.0,
    )
    input_model = InvoiceLookupInput
    output_model = InvoicesOutput

    async def execute(
        self, arguments: InvoiceLookupInput, context: ToolExecutionContext
    ) -> InvoicesOutput:
        invoices_repo = InvoiceRepository(self._session, context.organization_id)

        # A narrow business query: the caller says which thing they are asking
        # about, and exactly one of the two.
        if arguments.shipment_reference:
            invoices, shipment = await self._for_shipment(
                arguments.shipment_reference, context, invoices_repo
            )
            shipment_reference = shipment.reference
        elif arguments.customer_reference:
            invoices = await self._for_customer(
                arguments.customer_reference, context, invoices_repo
            )
            shipment_reference = None
        else:
            raise ToolValidationError(
                "Give either a shipment reference or a customer reference.",
                details={"fields": ["shipment_reference: missing", "customer_reference: missing"]},
            )

        return self._summarise(invoices, shipment_reference=shipment_reference)

    # -- Lookups ---------------------------------------------------------------

    async def _for_shipment(
        self, reference: str, context: ToolExecutionContext, invoices_repo: InvoiceRepository
    ) -> tuple[Sequence[Invoice], Shipment]:
        shipments = ShipmentRepository(self._session, context.organization_id)

        shipment = await shipments.get_by_reference(reference)
        if shipment is None:
            raise not_found("shipment")

        return await invoices_repo.list_for_shipment(shipment.id), shipment

    async def _for_customer(
        self, reference: str, context: ToolExecutionContext, invoices_repo: InvoiceRepository
    ) -> Sequence[Invoice]:
        customers = CustomerRepository(self._session, context.organization_id)

        customer = await customers.get_by_reference(reference)
        if customer is None:
            raise not_found("customer")

        return await invoices_repo.list_for_customer(customer.id)

    # -- Shaping ---------------------------------------------------------------

    def _summarise(
        self, invoices: Sequence[Invoice], *, shipment_reference: str | None
    ) -> InvoicesOutput:
        """Turn rows into the answer, doing the arithmetic here rather than in
        the prompt."""
        today = datetime.now(UTC).date()

        summaries = [
            InvoiceSummary(
                number=invoice.number,
                status=invoice.status,
                total=invoice.total,
                amount_paid=invoice.amount_paid,
                outstanding=invoice_outstanding(invoice),
                currency=invoice.currency,
                due_date=invoice.due_date,
                overdue=is_overdue(invoice, today=today),
                shipment_reference=shipment_reference if invoice.shipment_id else None,
            )
            for invoice in invoices
        ]

        owed = [summary for summary in summaries if summary.outstanding > ZERO]
        if not owed:
            return InvoicesOutput(invoices=summaries, total_outstanding=ZERO, currency=None)

        currencies = {summary.currency for summary in owed}
        if len(currencies) > 1:
            # No exchange rate exists in this system, so a total across
            # currencies would be a confident-looking number with no meaning.
            raise MixedCurrencyError()

        total = sum((summary.outstanding for summary in owed), start=ZERO)
        return InvoicesOutput(
            invoices=summaries, total_outstanding=total, currency=currencies.pop()
        )
