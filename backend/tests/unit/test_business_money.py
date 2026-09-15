"""The money rules: what is owed, and what must never be added together.

Pure functions over unsaved ORM objects. No database, no network.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.business import Invoice, ShipmentCharge
from app.models.enums import ChargeStatus, ChargeType, InvoiceStatus
from app.tools.business.money import (
    ZERO,
    MixedCurrencyError,
    invoice_outstanding,
    is_outstanding,
    is_overdue,
    total_outstanding,
)

TODAY = date(2026, 9, 15)


def charge(
    amount: str, status: ChargeStatus = ChargeStatus.OUTSTANDING, currency: str = "GBP"
) -> ShipmentCharge:
    return ShipmentCharge(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        shipment_id=uuid.uuid4(),
        charge_type=ChargeType.FREIGHT,
        amount=Decimal(amount),
        currency=currency,
        status=status,
    )


def invoice(
    total: str,
    paid: str,
    *,
    status: InvoiceStatus = InvoiceStatus.ISSUED,
    due: date | None = None,
    currency: str = "GBP",
) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        number="INV-1",
        customer_id=uuid.uuid4(),
        status=status,
        subtotal=Decimal(total),
        tax=ZERO,
        total=Decimal(total),
        amount_paid=Decimal(paid),
        currency=currency,
        due_date=due,
    )


# -- Which charges count ------------------------------------------------------


def test_only_outstanding_charges_count() -> None:
    """The whole rule: status decides, nothing else."""
    assert is_outstanding(charge("10.00", ChargeStatus.OUTSTANDING))
    assert not is_outstanding(charge("10.00", ChargeStatus.PAID))
    assert not is_outstanding(charge("10.00", ChargeStatus.WAIVED))


# -- Totalling ----------------------------------------------------------------


def test_no_charges_owes_nothing() -> None:
    total, currency = total_outstanding([])

    assert total == ZERO
    assert currency is None, "there is no currency to name for zero"


def test_a_single_outstanding_charge() -> None:
    total, currency = total_outstanding([charge("1250.00")])

    assert total == Decimal("1250.00")
    assert currency == "GBP"


def test_several_charges_are_added_exactly() -> None:
    """Decimal, not float: 0.10 + 0.20 + 0.30 must be 0.60 and not 0.6000000000000001."""
    total, _ = total_outstanding([charge("0.10"), charge("0.20"), charge("0.30")])

    assert total == Decimal("0.60")
    assert str(total) == "0.60"


def test_paid_and_waived_charges_are_excluded_from_the_total() -> None:
    charges = [
        charge("1250.00", ChargeStatus.OUTSTANDING),
        charge("180.50", ChargeStatus.OUTSTANDING),
        charge("75.00", ChargeStatus.PAID),
        charge("40.00", ChargeStatus.WAIVED),
    ]

    total, _ = total_outstanding(charges)

    assert total == Decimal("1430.50")


def test_everything_settled_owes_nothing() -> None:
    total, currency = total_outstanding([charge("640.00", ChargeStatus.PAID)])

    assert total == ZERO
    assert currency is None


def test_charges_in_different_currencies_are_refused() -> None:
    """There is no exchange rate here, so a total across currencies would be a
    confident-looking number with no meaning."""
    with pytest.raises(MixedCurrencyError):
        total_outstanding([charge("100.00", currency="GBP"), charge("100.00", currency="USD")])


def test_a_settled_charge_in_another_currency_does_not_trigger_the_refusal() -> None:
    """Only what is owed has to agree."""
    total, currency = total_outstanding(
        [charge("100.00", currency="GBP"), charge("50.00", ChargeStatus.PAID, currency="USD")]
    )

    assert total == Decimal("100.00")
    assert currency == "GBP"


def test_the_mixed_currency_refusal_says_nothing_internal() -> None:
    assert "postgres" not in MixedCurrencyError.message.lower()
    assert MixedCurrencyError.code == "mixed_currency"


# -- Invoices -----------------------------------------------------------------


def test_an_unpaid_invoice_owes_its_total() -> None:
    assert invoice_outstanding(invoice("1716.60", "0.00")) == Decimal("1716.60")


def test_a_paid_invoice_owes_nothing() -> None:
    assert invoice_outstanding(invoice("768.00", "768.00")) == ZERO


def test_a_part_paid_invoice_owes_the_remainder() -> None:
    assert invoice_outstanding(invoice("600.00", "200.00")) == Decimal("400.00")


def test_an_overpayment_is_not_reported_as_negative_debt() -> None:
    """A credit is not money owed the other way round."""
    assert invoice_outstanding(invoice("100.00", "140.00")) == ZERO


# -- Overdue is derived, never stored -----------------------------------------


def test_an_unpaid_invoice_past_its_due_date_is_overdue() -> None:
    past = invoice("100.00", "0.00", due=TODAY - timedelta(days=1))

    assert is_overdue(past, today=TODAY)


def test_an_invoice_due_today_is_not_yet_overdue() -> None:
    assert not is_overdue(invoice("100.00", "0.00", due=TODAY), today=TODAY)


def test_a_paid_invoice_is_never_overdue() -> None:
    settled = invoice("100.00", "100.00", status=InvoiceStatus.PAID, due=TODAY - timedelta(days=30))

    assert not is_overdue(settled, today=TODAY)


def test_an_invoice_with_no_due_date_is_never_overdue() -> None:
    assert not is_overdue(invoice("100.00", "0.00", due=None), today=TODAY)


@pytest.mark.parametrize("status", [InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED])
def test_a_draft_or_cancelled_invoice_is_not_overdue(status: InvoiceStatus) -> None:
    """Neither is a demand for payment."""
    unbilled = invoice("100.00", "0.00", status=status, due=TODAY - timedelta(days=30))

    assert not is_overdue(unbilled, today=TODAY)


def test_overdue_depends_on_the_date_it_is_asked_about() -> None:
    """Which is why it is a parameter rather than a column."""
    due = invoice("100.00", "0.00", due=date(2026, 10, 1))

    assert not is_overdue(due, today=date(2026, 9, 30))
    assert is_overdue(due, today=date(2026, 10, 2))
