"""The platform's business tools.

Five capabilities over one organization's operational records - four that
only look, and one that acts:

    get_shipment          where a consignment is
    get_shipment_charges  what is billed against it, and what is still owed
    get_customer          who the account is, and how much is open
    get_invoices          what has been billed to a shipment or a customer
    cancel_shipment       stop a consignment - destructive, and gated on a person

All four sit behind the Part 13 framework, so each one gets argument
validation, a deadline, cancellation, result validation and error normalisation
without implementing any of it. None of them contains a query: a tool asks a
repository, and the repository is the only thing that talks to the database.

Tenancy is not a convention here, it is the construction order. A tool holds a
session; it builds its repositories inside `execute`, from the organization on
the trusted execution context. There is no moment at which a tool holds a
repository scoped to a tenant it was not asked to act for.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.business.base import BusinessRecordNotFoundError, BusinessTool
from app.tools.business.customer import GetCustomerTool
from app.tools.business.invoice import GetInvoicesTool
from app.tools.business.money import MixedCurrencyError
from app.tools.business.shipment import (
    CancelShipmentTool,
    GetShipmentChargesTool,
    GetShipmentTool,
    ShipmentNotCancellableError,
)
from app.tools.registry import ToolRegistry


def build_business_registry(session: AsyncSession) -> ToolRegistry:
    """The production registry, bound to one request's session.

    Built per request rather than cached on the application, because these
    tools read the database and must take part in the caller's transaction -
    which is also what lets an integration test roll the whole thing back.
    """
    return ToolRegistry(
        [
            GetShipmentTool(session),
            GetShipmentChargesTool(session),
            GetCustomerTool(session),
            GetInvoicesTool(session),
            CancelShipmentTool(session),
        ]
    )


__all__ = [
    "BusinessRecordNotFoundError",
    "BusinessTool",
    "CancelShipmentTool",
    "GetCustomerTool",
    "GetInvoicesTool",
    "GetShipmentChargesTool",
    "GetShipmentTool",
    "MixedCurrencyError",
    "ShipmentNotCancellableError",
    "build_business_registry",
]
