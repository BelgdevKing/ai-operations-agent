"""Money, and the small deterministic rules that use it.

Everything here is `Decimal`. Not once a float: binary floating point cannot
represent 0.10, so summing three charges of 0.10 gives 0.30000000000000004, and
an invoice that is out by a penny is worse than one that fails loudly.

These rules live in Python rather than in the prompt on purpose. A language
model asked to add up charges will usually get it right, and "usually" is not a
property anyone wants in a figure a customer is asked to pay.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from app.models.business import Invoice, ShipmentCharge
from app.models.enums import ChargeStatus, InvoiceStatus
from app.tools.exceptions import ToolExecutionError

ZERO = Decimal("0.00")


class MixedCurrencyError(ToolExecutionError):
    """Amounts in different currencies were about to be added together.

    Refused rather than summed. There is no exchange rate in this system, so any
    total across currencies would be a number with no meaning - and a wrong
    total presented confidently is the worst possible outcome here.
    """

    code = "mixed_currency"
    message = "These amounts are in more than one currency and cannot be totalled."


def is_outstanding(charge: ShipmentCharge) -> bool:
    """Whether a charge is still owed.

    The whole rule: a charge counts if, and only if, its status says so. Paid
    and waived charges do not.
    """
    return charge.status is ChargeStatus.OUTSTANDING


def total_outstanding(charges: Iterable[ShipmentCharge]) -> tuple[Decimal, str | None]:
    """What is still owed across *charges*, and in which currency.

    Returns zero and no currency when nothing is outstanding - an ordinary
    answer, not an error.

    Raises:
        MixedCurrencyError: The outstanding charges are not all in one currency.
    """
    owed = [charge for charge in charges if is_outstanding(charge)]
    if not owed:
        return ZERO, None

    currencies = {charge.currency for charge in owed}
    if len(currencies) > 1:
        raise MixedCurrencyError()

    total = sum((charge.amount for charge in owed), start=ZERO)
    return total, currencies.pop()


def invoice_outstanding(invoice: Invoice) -> Decimal:
    """What is still owed on an invoice.

    Floored at zero: an overpayment is a credit, not a negative debt, and
    reporting "-40.00 outstanding" would read as money owed the other way.
    """
    return max(invoice.total - invoice.amount_paid, ZERO)


def is_overdue(invoice: Invoice, *, today: date) -> bool:
    """Whether an invoice is past its due date with money still owed.

    Derived rather than stored. An ``overdue`` status column would be correct
    on the day it was written and wrong the following morning; *today* is a
    parameter so the rule is testable without waiting.
    """
    if invoice.due_date is None:
        return False
    if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED, InvoiceStatus.DRAFT):
        return False

    return invoice.due_date < today and invoice_outstanding(invoice) > ZERO
