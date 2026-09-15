"""The production tool registry and what it advertises.

No database: a business tool holds a session but does not touch it until
`execute`, so the registry can be built and inspected without one.
"""

from __future__ import annotations

import pytest

from app.tools.business import build_business_registry
from app.tools.exceptions import ToolNotFoundError, ToolRegistrationError
from app.tools.models import RESERVED_ARGUMENT_NAMES, ToolSafety
from app.tools.registry import ToolRegistry

READ_ONLY = {"get_shipment", "get_shipment_charges", "get_customer", "get_invoices"}

# The one tool that changes anything. Kept separate below because almost every
# policy assertion in this file is about the read-only set, and a destructive
# tool passing a "nothing here needs approval" check would be the failure this
# file exists to catch.
ACTING = {"cancel_shipment"}

EXPECTED = READ_ONLY | ACTING


def registry() -> ToolRegistry:
    # None stands in for the session: nothing here reaches it.
    return build_business_registry(None)  # type: ignore[arg-type]


def test_every_business_tool_is_registered() -> None:
    assert {metadata.name for metadata in registry().describe()} == EXPECTED


def test_each_tool_resolves_by_its_stable_name() -> None:
    built = registry()

    for name in EXPECTED:
        assert built.resolve(name).metadata.name == name


def test_an_unknown_tool_is_refused() -> None:
    with pytest.raises(ToolNotFoundError):
        registry().resolve("drop_database")


def test_registering_the_same_tool_twice_is_refused() -> None:
    """A silent overwrite is how a deployment ends up running something nobody
    thinks is installed."""
    built = registry()

    from app.tools.business.shipment import GetShipmentTool

    with pytest.raises(ToolRegistrationError):
        built.register(GetShipmentTool(None))  # type: ignore[arg-type]


def test_building_the_registry_twice_produces_no_duplicates() -> None:
    assert len(registry()) == len(EXPECTED)
    assert len(registry()) == len(EXPECTED)


# -- Metadata policy ----------------------------------------------------------


def test_the_lookup_tools_are_read_only() -> None:
    for metadata in registry().describe():
        if metadata.name in READ_ONLY:
            assert metadata.safety is ToolSafety.READ_ONLY, metadata.name


def test_no_read_only_tool_requires_approval() -> None:
    """Looking something up should not need a person to agree to it."""
    for metadata in registry().describe():
        if metadata.name in READ_ONLY:
            assert not metadata.requires_approval, metadata.name
            assert metadata.auto_execute, metadata.name


def test_every_read_only_tool_is_eligible_for_retry() -> None:
    """Which follows from being read-only: running one twice changes nothing."""
    for metadata in registry().describe():
        if metadata.name in READ_ONLY:
            assert metadata.retry_allowed, metadata.name


def test_cancelling_a_shipment_is_destructive_and_gated() -> None:
    """The one tool that changes anything, and everything that follows from it.

    Stated as four separate assertions because each is a different property a
    future change could break independently - and because "destructive" with
    ``auto_execute`` true would mean the platform cancels consignments on a
    model's say-so.
    """
    metadata = registry().resolve("cancel_shipment").metadata

    assert metadata.safety is ToolSafety.DESTRUCTIVE
    assert metadata.requires_approval
    assert not metadata.auto_execute
    assert not metadata.retry_allowed, "a cancellation repeated is a second cancellation"


def test_nothing_destructive_can_be_declared_without_approval() -> None:
    """The invariant behind the tool above, checked at the type rather than the
    instance: it is not possible to write one that skips the gate."""
    from pydantic import ValidationError

    from app.tools.models import ToolMetadata

    with pytest.raises(ValidationError):
        ToolMetadata(
            name="delete_everything",
            description="x" * 50,
            safety=ToolSafety.DESTRUCTIVE,
            requires_approval=False,
        )


def test_every_business_tool_has_a_deadline() -> None:
    for metadata in registry().describe():
        assert metadata.timeout_seconds is not None, metadata.name
        assert 0 < metadata.timeout_seconds <= 30


def test_every_business_tool_is_organization_scoped() -> None:
    for metadata in registry().describe():
        assert metadata.organization_scoped, metadata.name


def test_every_tool_describes_itself_for_a_model_to_read() -> None:
    for metadata in registry().describe():
        assert len(metadata.description) > 40, metadata.name


def test_no_input_schema_accepts_an_identity_field() -> None:
    """The framework rejects these at call time; none is even declared."""
    built = registry()

    for metadata in built.describe():
        fields = set(built.resolve(metadata.name).input_model.model_fields)
        assert not fields & RESERVED_ARGUMENT_NAMES, metadata.name


def test_listed_metadata_exposes_no_implementation() -> None:
    """Safe to hand to a client, or to a model."""
    described = registry().describe()[0].model_dump()

    assert "session" not in described
    assert "input_model" not in described
    assert "output_model" not in described
