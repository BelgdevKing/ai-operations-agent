"""The workflow document: what it accepts, and everything it refuses.

No database and no engine. This is the shape of the thing a user of the platform
uploads, and most of the tests below are about what that shape makes
*impossible* - because a definition is data somebody wrote, and the moment it
can express computation it is a way to run code on somebody else's server.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.demo.workflows import shipment_exception_definition
from app.models.enums import WorkflowStepType
from app.workflows.definition import (
    AgentStep,
    ApprovalStep,
    Condition,
    ConditionOperator,
    ConditionStep,
    ToolCallStep,
    WorkflowDefinition,
)
from app.workflows.references import (
    ReferenceError,
    ReferenceRoot,
    looks_like_reference,
    parse_reference,
)


def one_step(**overrides: object) -> dict[str, object]:
    step: dict[str, object] = {
        "id": "only",
        "type": "tool_call",
        "tool": "get_shipment",
        "arguments": {},
        "next": None,
    }
    step.update(overrides)
    return step


def workflow(*steps: dict[str, object], entry: str = "only") -> dict[str, object]:
    return {"entry": entry, "steps": list(steps) or [one_step()]}


# -- The document ------------------------------------------------------------


def test_a_minimal_workflow_parses() -> None:
    definition = WorkflowDefinition.model_validate(workflow())

    assert definition.entry == "only"
    assert len(definition.steps) == 1
    assert definition.step("only").type is WorkflowStepType.TOOL_CALL


def test_the_demonstration_workflow_parses() -> None:
    """The document in `app/demo/workflows.py` is the shape a user writes."""
    definition = WorkflowDefinition.model_validate(shipment_exception_definition())

    assert definition.entry == "get_shipment"
    assert [step.id for step in definition.steps] == [
        "get_shipment",
        "get_charges",
        "is_exception",
        "cancel",
        "confirm",
    ]


def test_every_step_type_is_representable() -> None:
    definition = WorkflowDefinition.model_validate(
        workflow(
            one_step(id="a", next="b"),
            {
                "id": "b",
                "type": "agent_step",
                "agent_id": "00000000-0000-4000-8000-00000000a9e7",
                "message": "Summarise it.",
                "next": "c",
            },
            {
                "id": "c",
                "type": "condition",
                "condition": {"field": "$.input.x", "operator": "exists"},
                "on_true": "d",
                "on_false": None,
            },
            {"id": "d", "type": "approval", "reason": "Somebody should look.", "next": None},
            entry="a",
        )
    )

    assert [type(step) for step in definition.steps] == [
        ToolCallStep,
        AgentStep,
        ConditionStep,
        ApprovalStep,
    ]


def test_an_empty_workflow_is_refused() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({"entry": "a", "steps": []})


def test_duplicate_step_ids_are_refused() -> None:
    with pytest.raises(ValidationError, match="share the id"):
        WorkflowDefinition.model_validate(
            workflow(one_step(id="a", next=None), one_step(id="a", next=None), entry="a")
        )


def test_an_entry_that_is_not_a_step_is_refused() -> None:
    with pytest.raises(ValidationError, match="entry step"):
        WorkflowDefinition.model_validate(workflow(entry="nowhere"))


def test_a_transition_to_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        WorkflowDefinition.model_validate(workflow(one_step(next="elsewhere")))


def test_an_unknown_step_type_is_refused() -> None:
    """The discriminator is closed, so there is no type the engine has to guess at."""
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(workflow({"id": "only", "type": "shell", "next": None}))


def test_an_unknown_field_on_a_step_is_refused() -> None:
    # extra="forbid": a field nobody reads is a field somebody believes in.
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(workflow(one_step(command="rm -rf /")))


def test_a_step_id_must_be_an_identifier() -> None:
    for bad in ("Step One", "../etc", "a" * 70, "1step", ""):
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate(workflow(one_step(id=bad), entry=bad))


# -- Nothing executes --------------------------------------------------------


def test_a_definition_cannot_name_code_to_run() -> None:
    """Structural: there is no field for it in any step type.

    The nearest thing to an instruction a workflow can give is "call this
    registered tool" or "run this configured agent", both of which are names
    resolved against a registry the server controls.
    """
    for forbidden in ("expression", "code", "script", "python", "command", "callable", "eval"):
        for step_type in (ToolCallStep, AgentStep, ConditionStep, ApprovalStep):
            assert forbidden not in step_type.model_fields, (step_type.__name__, forbidden)


def test_an_expression_in_a_condition_is_not_an_expression() -> None:
    """A condition's field is a reference, and a reference is not code.

    Anything that is not one of the two supported reference shapes is refused
    when the workflow is written, so there is nothing to evaluate later.
    """
    for attempt in (
        "__import__('os').system('id')",
        "1 + 1",
        "$.input.x == 1",
        "${input.x}",
        "$.globals.secret",
        "$.steps.a.config.token",
    ):
        with pytest.raises(ValidationError):
            Condition(field=attempt, operator=ConditionOperator.EXISTS)


def test_a_dunder_path_is_well_formed_and_reaches_nothing() -> None:
    """``$.steps.a.output.__class__`` parses, and that is not a hole.

    A path segment is a key, and resolution walks dictionaries and lists only -
    never attributes - so this finds no key called ``__class__`` and answers the
    same as any other miss. Refusing the *name* would suggest the protection is
    a denylist, which is exactly the impression not to give: it is the absence
    of attribute access, and it holds for names nobody thought to list.
    """
    from app.workflows.references import MISSING, resolve

    reference = parse_reference("$.steps.a.output.__class__")
    resolved = resolve(reference, {"steps": {"a": {"output": {"status": "ok"}}}})

    assert resolved is MISSING


def test_a_tool_argument_that_looks_like_a_reference_must_be_one() -> None:
    with pytest.raises(ValidationError, match="argument 'reference'"):
        ToolCallStep(id="a", tool="get_shipment", arguments={"reference": "$.nowhere.x"})


def test_a_plain_string_argument_is_a_literal() -> None:
    """Only `$.` makes a value a reference, so ordinary text needs no escaping."""
    step = ToolCallStep(id="a", tool="get_shipment", arguments={"reference": "ABC123"})

    assert step.references == ()
    assert step.arguments["reference"] == "ABC123"


def test_an_approval_reason_cannot_be_a_reference() -> None:
    """A prompt assembled from business data is a way to put whatever a record
    contains in front of somebody as if the platform were saying it."""
    with pytest.raises(ValidationError, match="literal text"):
        ApprovalStep(id="a", reason="$.steps.get_shipment.output.shipment.notes")


def test_an_agent_step_cannot_choose_a_model_or_a_provider() -> None:
    for forbidden in ("model", "provider", "temperature", "max_output_tokens", "instructions"):
        assert forbidden not in AgentStep.model_fields


def test_no_step_can_name_an_organization() -> None:
    for step_type in (ToolCallStep, AgentStep, ConditionStep, ApprovalStep):
        for forbidden in ("organization_id", "user_id", "tenant_id", "run_id"):
            assert forbidden not in step_type.model_fields


# -- Conditions --------------------------------------------------------------


@pytest.mark.parametrize("operator", list(ConditionOperator))
def test_every_operator_is_usable(operator: ConditionOperator) -> None:
    value: object = None
    if operator in {ConditionOperator.IN, ConditionOperator.NOT_IN}:
        value = ["a", "b"]
    elif operator in {
        ConditionOperator.GREATER_THAN,
        ConditionOperator.GREATER_THAN_OR_EQUAL,
        ConditionOperator.LESS_THAN,
        ConditionOperator.LESS_THAN_OR_EQUAL,
    }:
        value = 10
    elif operator not in {ConditionOperator.EXISTS, ConditionOperator.NOT_EXISTS}:
        value = "x"

    condition = Condition(field="$.input.x", operator=operator, value=value)

    assert condition.operator is operator


def test_an_unknown_operator_is_refused() -> None:
    with pytest.raises(ValidationError):
        Condition(field="$.input.x", operator="matches_regex", value=".*")


def test_exists_takes_no_value() -> None:
    with pytest.raises(ValidationError, match="takes no value"):
        Condition(field="$.input.x", operator=ConditionOperator.EXISTS, value="x")


def test_in_takes_a_non_empty_list() -> None:
    with pytest.raises(ValidationError, match="non-empty list"):
        Condition(field="$.input.x", operator=ConditionOperator.IN, value=[])
    with pytest.raises(ValidationError, match="non-empty list"):
        Condition(field="$.input.x", operator=ConditionOperator.IN, value="a")


def test_equals_does_not_take_a_list() -> None:
    with pytest.raises(ValidationError, match="not a list"):
        Condition(field="$.input.x", operator=ConditionOperator.EQUALS, value=["a"])


def test_ordering_operators_compare_numbers() -> None:
    with pytest.raises(ValidationError, match="compares numbers"):
        Condition(field="$.input.x", operator=ConditionOperator.GREATER_THAN, value="ten")


def test_a_comparison_list_is_bounded() -> None:
    with pytest.raises(ValidationError, match="at most"):
        Condition(field="$.input.x", operator=ConditionOperator.IN, value=list(range(200)))


# -- References --------------------------------------------------------------


def test_an_input_reference_parses() -> None:
    reference = parse_reference("$.input.shipment_reference")

    assert reference.root is ReferenceRoot.INPUT
    assert reference.step_id is None
    assert reference.path == ("shipment_reference",)
    assert reference.render() == "$.input.shipment_reference"


def test_a_step_reference_parses() -> None:
    reference = parse_reference("$.steps.get_shipment.output.shipment.status")

    assert reference.root is ReferenceRoot.STEPS
    assert reference.step_id == "get_shipment"
    assert reference.path == ("shipment", "status")
    assert reference.render() == "$.steps.get_shipment.output.shipment.status"


@pytest.mark.parametrize(
    "text",
    [
        "input.x",
        "$.",
        "$.nowhere.x",
        "$.steps.get_shipment",
        "$.steps.get_shipment.result.x",
        "$.steps.Get_Shipment.output.x",
        "$.input.a.b[0]",
        "$.input." + ".".join("x" * 20 for _ in range(20)),
    ],
)
def test_a_malformed_reference_is_refused(text: str) -> None:
    with pytest.raises(ReferenceError):
        parse_reference(text)


def test_only_the_prefix_makes_something_a_reference() -> None:
    assert looks_like_reference("$.input.x")
    assert not looks_like_reference("ABC123")
    assert not looks_like_reference("$input.x")
    assert not looks_like_reference(42)


def test_a_step_exposes_the_references_it_reads() -> None:
    step = ToolCallStep(
        id="a",
        tool="get_shipment",
        arguments={"reference": "$.steps.b.output.x", "note": "literal"},
    )

    assert [reference.step_id for reference in step.references] == ["b"]


def test_transitions_are_what_the_graph_walks() -> None:
    condition = ConditionStep(
        id="c",
        condition=Condition(field="$.input.x", operator=ConditionOperator.EXISTS),
        on_true="a",
        on_false="b",
    )
    approval = ApprovalStep(id="d", reason="Because.", next="a", on_reject="b")
    ending = ToolCallStep(id="e", tool="get_shipment", next=None)

    assert set(condition.transitions) == {"a", "b"}
    assert set(approval.transitions) == {"a", "b"}
    assert ending.transitions == (), "a step with nowhere to go ends the workflow"
