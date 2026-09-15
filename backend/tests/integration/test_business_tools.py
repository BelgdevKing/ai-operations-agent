"""The business tools against a real database.

Seeded with the same dataset a developer loads locally, inside the test's
transaction, so nothing survives the test. The two demo organizations
deliberately share a customer reference and a shipment reference with entirely
different records behind them - which is what makes the isolation tests here
about something real rather than about two strings that happen to differ.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.demo.seed import seed_demo_data
from app.models.enums import ShipmentStatus
from app.repositories.customer import CustomerRepository
from app.repositories.invoice import InvoiceRepository
from app.repositories.shipment import ShipmentChargeRepository, ShipmentRepository
from app.tools.business import build_business_registry
from app.tools.executor import ToolExecutor
from app.tools.models import ToolOutcome, ToolRequest, ToolResult

pytestmark = pytest.mark.integration

NORTHWIND = "demo-northwind"
GLOBEX = "demo-globex"


@pytest.fixture
async def demo(session: AsyncSession) -> AsyncIterator[dict[str, uuid.UUID]]:
    """The demo dataset, written inside the test's transaction."""
    organizations = await seed_demo_data(session)
    yield organizations


def executor_for(session: AsyncSession) -> ToolExecutor:
    return ToolExecutor(build_business_registry(session), Settings(app_env="test"))


async def call(
    session: AsyncSession,
    organization_id: uuid.UUID,
    tool: str,
    arguments: dict[str, object] | None = None,
) -> ToolResult:
    """Run a tool the way the runtime does: identity from outside the request."""
    return await executor_for(session).execute(
        ToolRequest(tool_name=tool, arguments=arguments or {}),
        organization_id=organization_id,
        user_id=uuid.uuid4(),
    )


# -- Repositories -------------------------------------------------------------


async def test_a_shipment_is_found_by_its_reference(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    shipments = ShipmentRepository(session, demo[NORTHWIND])

    shipment = await shipments.get_by_reference("ABC123")

    assert shipment is not None
    assert shipment.status is ShipmentStatus.IN_TRANSIT
    assert shipment.origin == "London, GB"


async def test_the_same_reference_in_another_organization_is_a_different_shipment(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Both organizations have an ABC123. They are unrelated records."""
    northwind = await ShipmentRepository(session, demo[NORTHWIND]).get_by_reference("ABC123")
    globex = await ShipmentRepository(session, demo[GLOBEX]).get_by_reference("ABC123")

    assert northwind is not None and globex is not None
    assert northwind.id != globex.id
    assert northwind.destination == "Hamburg, DE"
    assert globex.destination == "Denver, US"


async def test_an_unknown_reference_is_simply_absent(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    assert await ShipmentRepository(session, demo[NORTHWIND]).get_by_reference("NOPE") is None


async def test_a_customer_is_found_by_reference(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    customer = await CustomerRepository(session, demo[NORTHWIND]).get_by_reference("CUST-1001")

    assert customer is not None
    assert customer.name == "Acme Industries"


async def test_the_same_customer_reference_is_a_different_company_elsewhere(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    theirs = await CustomerRepository(session, demo[GLOBEX]).get_by_reference("CUST-1001")

    assert theirs is not None
    assert theirs.name == "Initech Supplies"


async def test_charges_are_scoped_to_the_organization(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Even given another tenant's shipment id, a scoped repository returns nothing."""
    theirs = await ShipmentRepository(session, demo[GLOBEX]).get_by_reference("ABC123")
    assert theirs is not None

    charges = await ShipmentChargeRepository(session, demo[NORTHWIND]).list_for_shipment(theirs.id)

    assert list(charges) == []


async def test_invoices_are_scoped_to_the_organization(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    theirs = await CustomerRepository(session, demo[GLOBEX]).get_by_reference("CUST-1001")
    assert theirs is not None

    invoices = await InvoiceRepository(session, demo[NORTHWIND]).list_for_customer(theirs.id)

    assert list(invoices) == []


# -- The database refuses cross-tenant relationships --------------------------


async def test_a_charge_cannot_be_attached_to_another_organizations_shipment(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The composite foreign key, doing its job.

    Not a repository rule and not an API rule: the row is rejected by the
    database, so a cross-tenant relationship is unrepresentable rather than
    merely untested.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.business import ShipmentCharge
    from app.models.enums import ChargeType

    theirs = await ShipmentRepository(session, demo[GLOBEX]).get_by_reference("XYZ999")
    assert theirs is not None

    session.add(
        ShipmentCharge(
            organization_id=demo[NORTHWIND],  # one tenant
            shipment_id=theirs.id,  # another tenant's shipment
            charge_type=ChargeType.FREIGHT,
            amount=Decimal("1.00"),
            currency="GBP",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


# -- get_shipment -------------------------------------------------------------


async def test_get_shipment_returns_the_operational_picture(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "ABC123"})

    assert result.ok, result.failure
    shipment = result.data["shipment"]  # type: ignore[index]
    assert shipment["shipment_reference"] == "ABC123"
    assert shipment["status"] == "in_transit"
    assert shipment["destination"] == "Hamburg, DE"
    assert shipment["customer"]["name"] == "Acme Industries"


async def test_get_shipment_for_another_tenants_reference_is_not_found(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Globex's XYZ999 exists. Northwind must not learn that."""
    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "XYZ999"})

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "shipment_not_found"


async def test_a_missing_shipment_and_another_tenants_answer_identically(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Otherwise the difference would confirm that somebody else's exists."""
    absent = await call(
        session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "NO-SUCH-THING"}
    )
    theirs = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "XYZ999"})

    assert absent.failure is not None and theirs.failure is not None
    assert absent.failure.code == theirs.failure.code
    assert absent.failure.message == theirs.failure.message


async def test_the_not_found_message_names_no_organization(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "XYZ999"})

    serialised = str(result.model_dump())
    assert str(demo[GLOBEX]) not in serialised
    assert "globex" not in serialised.lower()


async def test_each_organization_sees_its_own_record_for_a_shared_reference(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """The headline isolation case: one reference, two tenants, two answers."""
    ours = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "ABC123"})
    theirs = await call(session, demo[GLOBEX], "get_shipment", {"shipment_reference": "ABC123"})

    assert ours.data["shipment"]["destination"] == "Hamburg, DE"  # type: ignore[index]
    assert theirs.data["shipment"]["destination"] == "Denver, US"  # type: ignore[index]


# -- get_shipment_charges -----------------------------------------------------


async def test_outstanding_charges_are_totalled_by_the_application(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """1250.00 + 180.50 outstanding; the 75.00 handling charge is paid."""
    result = await call(
        session, demo[NORTHWIND], "get_shipment_charges", {"shipment_reference": "ABC123"}
    )

    assert result.ok, result.failure
    assert len(result.data["charges"]) == 3  # type: ignore[index]
    assert Decimal(str(result.data["total_outstanding"])) == Decimal("1430.50")  # type: ignore[index]
    assert result.data["currency"] == "GBP"  # type: ignore[index]


async def test_a_settled_shipment_owes_nothing(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_shipment_charges", {"shipment_reference": "DEF456"}
    )

    assert result.ok
    assert Decimal(str(result.data["total_outstanding"])) == Decimal("0")  # type: ignore[index]
    assert result.data["currency"] is None  # type: ignore[index]


async def test_a_shipment_with_no_charges_is_a_normal_answer(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Empty, not a failure."""
    result = await call(
        session, demo[NORTHWIND], "get_shipment_charges", {"shipment_reference": "GHI789"}
    )

    assert result.ok
    assert result.data["charges"] == []  # type: ignore[index]
    assert Decimal(str(result.data["total_outstanding"])) == Decimal("0")  # type: ignore[index]


async def test_money_is_never_a_float(session: AsyncSession, demo: dict[str, uuid.UUID]) -> None:
    """Serialised exactly, so 180.50 does not become 180.49999999999999."""
    result = await call(
        session, demo[NORTHWIND], "get_shipment_charges", {"shipment_reference": "ABC123"}
    )

    for charge in result.data["charges"]:  # type: ignore[index]
        assert not isinstance(charge["amount"], float)
    assert not isinstance(result.data["total_outstanding"], float)  # type: ignore[index]
    assert Decimal(str(result.data["total_outstanding"])) == Decimal("1430.50")  # type: ignore[index]


async def test_charges_for_another_tenants_shipment_are_not_found(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_shipment_charges", {"shipment_reference": "XYZ999"}
    )

    assert result.failure is not None
    assert result.failure.code == "shipment_not_found"


# -- get_customer -------------------------------------------------------------


async def test_get_customer_returns_operational_detail(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_customer", {"customer_reference": "CUST-1001"}
    )

    assert result.ok, result.failure
    assert result.data["customer"]["name"] == "Acme Industries"  # type: ignore[index]
    # ABC123 is in transit and JKL012 is held up; neither has arrived.
    assert result.data["open_shipments"] == 2  # type: ignore[index]


async def test_get_customer_is_scoped_to_the_organization(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[GLOBEX], "get_customer", {"customer_reference": "CUST-1001"})

    assert result.data["customer"]["name"] == "Initech Supplies"  # type: ignore[index]


async def test_an_unknown_customer_is_not_found(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_customer", {"customer_reference": "CUST-9999"}
    )

    assert result.failure is not None
    assert result.failure.code == "customer_not_found"


# -- get_invoices -------------------------------------------------------------


async def test_invoices_for_a_shipment(session: AsyncSession, demo: dict[str, uuid.UUID]) -> None:
    result = await call(session, demo[NORTHWIND], "get_invoices", {"shipment_reference": "ABC123"})

    assert result.ok, result.failure
    invoices = result.data["invoices"]  # type: ignore[index]
    assert len(invoices) == 1
    assert invoices[0]["number"] == "INV-2026-0001"
    assert Decimal(str(invoices[0]["outstanding"])) == Decimal("1716.60")
    assert invoices[0]["overdue"] is True, "issued, unpaid and past its due date"


async def test_invoices_for_a_customer_include_the_part_paid_one(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_invoices", {"customer_reference": "CUST-1001"}
    )

    assert result.ok
    numbers = {invoice["number"] for invoice in result.data["invoices"]}  # type: ignore[index]
    assert numbers == {"INV-2026-0001", "INV-2026-0003"}

    part_paid = next(
        i
        for i in result.data["invoices"]
        if i["number"] == "INV-2026-0003"  # type: ignore[index]
    )
    assert Decimal(str(part_paid["outstanding"])) == Decimal("400.00")
    assert part_paid["overdue"] is False, "not yet due"


async def test_a_paid_invoice_owes_nothing(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_invoices", {"shipment_reference": "DEF456"})

    invoice = result.data["invoices"][0]  # type: ignore[index]
    assert invoice["status"] == "paid"
    assert Decimal(str(invoice["outstanding"])) == Decimal("0")
    assert Decimal(str(result.data["total_outstanding"])) == Decimal("0")  # type: ignore[index]


async def test_a_shipment_with_no_invoice_returns_an_empty_list(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_invoices", {"shipment_reference": "GHI789"})

    assert result.ok
    assert result.data["invoices"] == []  # type: ignore[index]


async def test_invoices_needs_something_to_look_up(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_invoices", {})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


# -- Untrusted arguments ------------------------------------------------------


async def test_a_tenant_supplied_in_the_arguments_is_refused(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Part 13's reserved-name guard, reached through a real tool."""
    result = await call(
        session,
        demo[NORTHWIND],
        "get_shipment",
        {"shipment_reference": "XYZ999", "organization_id": str(demo[GLOBEX])},
    )

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"
    assert "organization_id" in result.failure.details["rejected_arguments"]


async def test_no_business_tool_declares_an_identity_argument(
    session: AsyncSession,
) -> None:
    """Structural: the schemas simply have nowhere to put one."""
    from app.tools.models import RESERVED_ARGUMENT_NAMES

    for metadata in build_business_registry(session).describe():
        tool = build_business_registry(session).resolve(metadata.name)
        assert not set(tool.input_model.model_fields) & RESERVED_ARGUMENT_NAMES


async def test_an_unexpected_argument_is_refused(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session,
        demo[NORTHWIND],
        "get_shipment",
        {"shipment_reference": "ABC123", "limit": "9999"},
    )

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


async def test_an_oversized_reference_is_refused_before_a_query(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "A" * 500})

    assert result.failure is not None
    assert result.failure.code == "tool_invalid_arguments"


# -- Nothing internal escapes -------------------------------------------------


async def test_a_database_failure_is_reported_without_its_detail(
    session: AsyncSession, demo: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver exception carries hosts, queries and sometimes credentials."""
    leaky = OperationalError(
        "SELECT * FROM shipments WHERE organization_id = $1",
        {},
        Exception("could not connect to db.internal:5432 user=aiops password=hunter2"),
    )

    async def explode(*args: object, **kwargs: object) -> None:
        raise leaky

    monkeypatch.setattr(ShipmentRepository, "get_with_customer", explode)

    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "ABC123"})

    assert result.outcome is ToolOutcome.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_execution_failed"

    serialised = str(result.model_dump())
    assert "hunter2" not in serialised
    assert "db.internal" not in serialised
    assert "SELECT" not in serialised


async def test_a_tool_result_carries_no_internal_identifiers(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    """Explicit output schemas, not serialised ORM rows."""
    result = await call(session, demo[NORTHWIND], "get_shipment", {"shipment_reference": "ABC123"})

    serialised = str(result.data)
    assert str(demo[NORTHWIND]) not in serialised, "no organization id"
    assert "organization_id" not in serialised
    assert "customer_id" not in serialised
    assert "created_at" not in serialised


async def test_a_customer_result_exposes_no_unnecessary_personal_detail(
    session: AsyncSession, demo: dict[str, uuid.UUID]
) -> None:
    result = await call(
        session, demo[NORTHWIND], "get_customer", {"customer_reference": "CUST-1001"}
    )

    fields = set(result.data or {})
    assert fields == {"customer", "email", "open_shipments"}
