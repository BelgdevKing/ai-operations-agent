"""The data a workflow carries, and everything it cannot reach.

Reference resolution, argument interpolation and condition evaluation, with no
database and no engine. Half of these are security tests wearing ordinary
clothes: the guarantee is that a reference is a lookup in a plain dictionary,
and the way to demonstrate that is to try the things an attacker would.
"""

from __future__ import annotations

import pytest

from app.workflows.context import WorkflowContext, depth_of, encoded_size, evaluate
from app.workflows.definition import Condition, ConditionOperator
from app.workflows.exceptions import WorkflowReferenceError
from app.workflows.references import MISSING, parse_reference, resolve


def context() -> WorkflowContext:
    built = WorkflowContext({"shipment_reference": "ABC123", "count": 3, "flag": True})
    built.record(
        "get_shipment",
        {"shipment": {"status": "exception", "customer": {"name": "Acme"}}, "charges": [1, 2, 3]},
    )
    return built


def value_of(reference: str) -> object:
    return context().resolve(parse_reference(reference))


# -- Reading -----------------------------------------------------------------


def test_input_is_readable() -> None:
    assert value_of("$.input.shipment_reference") == "ABC123"


def test_step_output_is_readable() -> None:
    assert value_of("$.steps.get_shipment.output.shipment.status") == "exception"


def test_a_nested_path_is_readable() -> None:
    assert value_of("$.steps.get_shipment.output.shipment.customer.name") == "Acme"


def test_a_list_is_indexable() -> None:
    assert value_of("$.steps.get_shipment.output.charges.1") == 2


def test_a_whole_object_is_readable() -> None:
    assert value_of("$.steps.get_shipment.output.shipment.customer") == {"name": "Acme"}


def test_a_missing_value_is_refused_rather_than_substituted() -> None:
    """A tool called with a silently missing argument is a tool called wrongly."""
    with pytest.raises(WorkflowReferenceError, match="not available"):
        value_of("$.input.nothing")


def test_a_reference_to_a_step_that_has_not_run_is_refused() -> None:
    with pytest.raises(WorkflowReferenceError):
        value_of("$.steps.later.output.x")


def test_walking_past_a_scalar_finds_nothing() -> None:
    with pytest.raises(WorkflowReferenceError):
        value_of("$.input.shipment_reference.length")


# -- What it cannot reach ----------------------------------------------------


def test_the_context_contains_only_input_and_step_output() -> None:
    """The literal answer to "what can a definition read?"."""
    assert set(context().data) == {"input", "steps"}


def test_identity_is_not_in_the_context() -> None:
    """Organization, user, request and run are execution context established by
    the server. They are not data a workflow may read, and they are not there."""
    data = context().data

    for forbidden in ("organization_id", "user_id", "request_id", "run_id"):
        assert forbidden not in data
        assert forbidden not in data["input"]


@pytest.mark.parametrize(
    "path",
    [
        "$.steps.get_shipment.output.__class__",
        "$.steps.get_shipment.output.__dict__",
        "$.input.__class__",
        "$.steps.get_shipment.output.shipment.__init__",
    ],
)
def test_attribute_access_is_never_attempted(path: str) -> None:
    """The walk indexes dictionaries and lists. It never gets an attribute, so a
    dunder path finds no key and answers like any other miss - and that holds
    for names nobody thought to put on a list."""
    assert resolve(parse_reference(path), context().data) is MISSING


def test_an_index_out_of_range_finds_nothing() -> None:
    assert resolve(parse_reference("$.steps.get_shipment.output.charges.99"), context().data) is (
        MISSING
    )


# -- Interpolation -----------------------------------------------------------


def test_references_are_substituted_and_literals_are_not() -> None:
    interpolated = context().interpolate(
        {"shipment_reference": "$.input.shipment_reference", "reason": "Customer asked."}
    )

    assert interpolated == {"shipment_reference": "ABC123", "reason": "Customer asked."}


def test_interpolation_is_substitution_and_not_formatting() -> None:
    """There is no template language: a value either is a reference or is used
    exactly as written, so nothing can be built by concatenation."""
    interpolated = context().interpolate(
        {"a": "prefix $.input.shipment_reference", "b": "$.input.count"}
    )

    assert interpolated["a"] == "prefix $.input.shipment_reference"
    assert interpolated["b"] == 3


def test_a_non_string_argument_passes_through() -> None:
    interpolated = context().interpolate({"limit": 10, "flags": ["a"], "nested": {"x": 1}})

    assert interpolated == {"limit": 10, "flags": ["a"], "nested": {"x": 1}}


def test_interpolating_a_missing_reference_refuses_the_step() -> None:
    with pytest.raises(WorkflowReferenceError):
        context().interpolate({"reference": "$.input.absent"})


# -- Conditions --------------------------------------------------------------


def evaluate_against(field: str, operator: ConditionOperator, value: object = None) -> bool:
    return evaluate(Condition(field=field, operator=operator, value=value), context())  # type: ignore[arg-type]


def test_equals_and_not_equals() -> None:
    field = "$.steps.get_shipment.output.shipment.status"

    assert evaluate_against(field, ConditionOperator.EQUALS, "exception")
    assert not evaluate_against(field, ConditionOperator.EQUALS, "delivered")
    assert evaluate_against(field, ConditionOperator.NOT_EQUALS, "delivered")


def test_exists_and_not_exists_do_not_fail_on_absence() -> None:
    assert evaluate_against("$.input.shipment_reference", ConditionOperator.EXISTS)
    assert not evaluate_against("$.input.absent", ConditionOperator.EXISTS)
    assert evaluate_against("$.input.absent", ConditionOperator.NOT_EXISTS)


def test_ordering_operators() -> None:
    assert evaluate_against("$.input.count", ConditionOperator.GREATER_THAN, 2)
    assert not evaluate_against("$.input.count", ConditionOperator.GREATER_THAN, 3)
    assert evaluate_against("$.input.count", ConditionOperator.GREATER_THAN_OR_EQUAL, 3)
    assert evaluate_against("$.input.count", ConditionOperator.LESS_THAN, 4)
    assert evaluate_against("$.input.count", ConditionOperator.LESS_THAN_OR_EQUAL, 3)


def test_ordering_against_a_non_number_is_false_rather_than_an_error() -> None:
    """ "Is this greater than 100" asked about a string is a condition that is
    not satisfied, not a reason to fail the whole workflow."""
    assert not evaluate_against("$.input.shipment_reference", ConditionOperator.GREATER_THAN, 100)


def test_a_boolean_is_not_a_number() -> None:
    """`True > 0` is true in Python and means nothing in a business rule."""
    assert not evaluate_against("$.input.flag", ConditionOperator.GREATER_THAN, 0)


def test_a_boolean_does_not_equal_one() -> None:
    """`True == 1` is true in Python, which would make a flag match a count."""
    assert not evaluate_against("$.input.flag", ConditionOperator.EQUALS, 1)
    assert evaluate_against("$.input.flag", ConditionOperator.EQUALS, True)


def test_in_and_not_in() -> None:
    field = "$.steps.get_shipment.output.shipment.status"

    assert evaluate_against(field, ConditionOperator.IN, ["exception", "cancelled"])
    assert not evaluate_against(field, ConditionOperator.IN, ["delivered"])
    assert evaluate_against(field, ConditionOperator.NOT_IN, ["delivered"])


def test_a_comparison_against_a_missing_value_is_refused() -> None:
    """Except for exists / not_exists, which are the operators for asking."""
    with pytest.raises(WorkflowReferenceError):
        evaluate_against("$.input.absent", ConditionOperator.EQUALS, "x")


# -- Rebuilding --------------------------------------------------------------


def test_a_context_rebuilds_from_what_was_stored() -> None:
    """How a resumed run knows what earlier steps produced without re-running
    them."""
    rebuilt = WorkflowContext.rebuild(
        {"shipment_reference": "ABC123"},
        {"get_shipment": {"shipment": {"status": "exception"}}},
    )

    assert rebuilt.resolve(parse_reference("$.input.shipment_reference")) == "ABC123"
    assert (
        rebuilt.resolve(parse_reference("$.steps.get_shipment.output.shipment.status"))
        == "exception"
    )


def test_recording_a_step_makes_it_readable() -> None:
    built = WorkflowContext()
    assert built.output_of("a") is None

    built.record("a", {"x": 1})

    assert built.output_of("a") == {"x": 1}


# -- Payload measurement -----------------------------------------------------


def test_size_is_measured_on_what_would_be_stored() -> None:
    assert encoded_size({}) == 2
    assert encoded_size({"a": "x" * 100}) > 100


def test_depth_counts_nesting() -> None:
    assert depth_of({}) == 1
    assert depth_of({"a": 1}) == 2
    assert depth_of({"a": {"b": {"c": 1}}}) == 4
    assert depth_of({"a": [{"b": 1}]}) == 4
