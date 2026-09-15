"""What a workflow *is*: a validated document, not a program.

A definition is data written by a user of the platform and stored as JSONB. It
describes a graph of typed steps and the transitions between them, and it can
express nothing else - no expressions, no loops, no code, no way to name a
module or a callable. The engine reads it; it never executes it.

    entry ──▶ tool_call ──▶ condition ──┬──▶ approval ──▶ tool_call ──▶ (end)
                                        └──▶ (end)

Four step types, discriminated by ``type`` so that adding a fifth is a new class
and a new handler rather than a new branch in half a dozen places:

``tool_call``   run a registered tool through the existing executor
``agent_step``  run a registered agent through the existing runtime
``condition``   branch on one declarative comparison
``approval``    stop and ask a person

The step-type names are ``app.models.enums.WorkflowStepType``'s, which the
``workflow_step_runs.step_type`` column already stores. One vocabulary for the
document, the column and the engine - a second set of friendlier names would
only be a mapping to get wrong.

Nothing in this module touches a database, a tool, an agent or a session. It is
the shape of the document and the rules a document must satisfy on its own; the
rules that need to know what exists - which tools, which agents - live in
:mod:`app.workflows.validation`.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import WorkflowStepType
from app.workflows.references import (
    STEP_ID_PATTERN,
    Reference,
    ReferenceError,
    looks_like_reference,
    parse_reference,
)

StepId = Annotated[str, Field(pattern=STEP_ID_PATTERN)]

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 1_000
MAX_REASON_LENGTH = 500
MAX_PROMPT_LENGTH = 4_000
MAX_ARGUMENTS = 20

# How many values an `in` / `not_in` comparison may list. Bounded because the
# whole document is bounded, not because a longer list would be dangerous.
MAX_CONDITION_VALUES = 50


class ConditionOperator(enum.StrEnum):
    """The complete set of comparisons a condition may make.

    Explicitly enumerated, and an unknown one fails validation rather than
    falling through to something permissive. This list is the entire reason a
    workflow cannot evaluate an arbitrary expression: there is no operator that
    takes code, and no operator that is not here.
    """

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    IN = "in"
    NOT_IN = "not_in"


#: Operators that compare against nothing - the value field must be absent.
UNARY_OPERATORS = frozenset({ConditionOperator.EXISTS, ConditionOperator.NOT_EXISTS})

#: Operators whose value is a list.
COLLECTION_OPERATORS = frozenset({ConditionOperator.IN, ConditionOperator.NOT_IN})

#: Operators that only make sense between two numbers.
ORDERING_OPERATORS = frozenset(
    {
        ConditionOperator.GREATER_THAN,
        ConditionOperator.GREATER_THAN_OR_EQUAL,
        ConditionOperator.LESS_THAN,
        ConditionOperator.LESS_THAN_OR_EQUAL,
    }
)

# What a condition may be compared against. Scalars and lists of scalars - not
# objects, because "is this object equal to that object" is a question nobody
# writing a business rule actually means to ask.
ConditionValue = str | int | float | bool | None
ComparableValue = ConditionValue | list[ConditionValue]


class Condition(BaseModel):
    """One declarative comparison.

    {"field": "$.steps.get_shipment.output.shipment.status",
     "operator": "equals",
     "value": "exception"}
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(description="A reference to the value being tested.")
    operator: ConditionOperator
    value: ComparableValue = Field(
        default=None, description="What to compare against. Absent for exists / not_exists."
    )

    @field_validator("field")
    @classmethod
    def _field_is_a_reference(cls, value: str) -> str:
        # Parsed rather than pattern-matched, so a malformed reference is
        # refused when the workflow is written rather than when it runs.
        parse_reference(value)
        return value

    @model_validator(mode="after")
    def _value_matches_the_operator(self) -> Self:
        if self.operator in UNARY_OPERATORS:
            if self.value is not None:
                raise ValueError(f"{self.operator.value} takes no value.")
            return self

        if self.operator in COLLECTION_OPERATORS:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.operator.value} takes a non-empty list.")
            if len(self.value) > MAX_CONDITION_VALUES:
                raise ValueError(
                    f"{self.operator.value} accepts at most {MAX_CONDITION_VALUES} values."
                )
            return self

        if isinstance(self.value, list):
            raise ValueError(f"{self.operator.value} takes a single value, not a list.")

        if self.operator in ORDERING_OPERATORS and not isinstance(self.value, int | float):
            raise ValueError(f"{self.operator.value} compares numbers.")

        return self

    @property
    def reference(self) -> Reference:
        return parse_reference(self.field)


class _BaseStep(BaseModel):
    """What every step has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: StepId = Field(description="Unique within the workflow, and stable across versions.")
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)

    @property
    def transitions(self) -> tuple[str, ...]:
        """Every step this one can hand control to. Empty means it ends the run."""
        return ()

    @property
    def references(self) -> tuple[Reference, ...]:
        """Every reference this step reads, for validation."""
        return ()


class ToolCallStep(_BaseStep):
    """Run a registered tool.

    ``arguments`` values may be references. That is data interpolation and
    nothing more: a value either starts with ``$.`` and is looked up, or it is
    used exactly as written. There is no template language, no concatenation and
    no expression - see :mod:`app.workflows.references`.
    """

    type: Literal[WorkflowStepType.TOOL_CALL] = WorkflowStepType.TOOL_CALL

    tool: str = Field(description="Name of a tool in the registry.")
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=MAX_ARGUMENTS)
    next: StepId | None = Field(default=None, description="Null ends the workflow.")

    @field_validator("arguments")
    @classmethod
    def _references_parse(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name, argument in value.items():
            if looks_like_reference(argument):
                try:
                    parse_reference(argument)
                except ReferenceError as exc:
                    raise ValueError(f"argument {name!r}: {exc}") from exc
        return value

    @property
    def transitions(self) -> tuple[str, ...]:
        return (self.next,) if self.next else ()

    @property
    def references(self) -> tuple[Reference, ...]:
        return tuple(
            parse_reference(argument)
            for argument in self.arguments.values()
            if looks_like_reference(argument)
        )


class AgentStep(_BaseStep):
    """Run a registered agent over one message.

    The workflow names an agent. It does not name a model, a provider, a
    temperature or a token budget - all of those are the agent's own
    server-side configuration, and a workflow that could set them would be a
    way for a definition to spend somebody else's money.
    """

    type: Literal[WorkflowStepType.AGENT_STEP] = WorkflowStepType.AGENT_STEP

    agent_id: str = Field(description="Id of an agent this organization may run.")
    message: str = Field(
        min_length=1,
        max_length=MAX_PROMPT_LENGTH,
        description="What to ask. A reference, or literal text.",
    )
    next: StepId | None = None

    @field_validator("message")
    @classmethod
    def _reference_parses(cls, value: str) -> str:
        if looks_like_reference(value):
            parse_reference(value)
        return value

    @property
    def transitions(self) -> tuple[str, ...]:
        return (self.next,) if self.next else ()

    @property
    def references(self) -> tuple[Reference, ...]:
        return (parse_reference(self.message),) if looks_like_reference(self.message) else ()


class ConditionStep(_BaseStep):
    """Branch on one comparison.

    Both branches are explicit. A condition with an implicit "otherwise carry
    on" would make the graph depend on step order, and a workflow whose meaning
    changes when somebody reorders the document is one nobody can review.
    """

    type: Literal[WorkflowStepType.CONDITION] = WorkflowStepType.CONDITION

    condition: Condition
    on_true: StepId | None = Field(default=None, description="Null ends the workflow.")
    on_false: StepId | None = Field(default=None, description="Null ends the workflow.")

    @property
    def transitions(self) -> tuple[str, ...]:
        return tuple(target for target in (self.on_true, self.on_false) if target)

    @property
    def references(self) -> tuple[Reference, ...]:
        return (self.condition.reference,)


class ApprovalStep(_BaseStep):
    """Stop and ask a person.

    Gates a decision rather than a specific execution. A *tool* that needs
    approval is gated by the tool framework at its own step, where the approval
    can name the exact execution it authorises - which is the stronger
    guarantee, and the one that makes "the approved action ran at most once"
    true. This step is for everything else: a go/no-go a business process needs
    before it continues.
    """

    type: Literal[WorkflowStepType.APPROVAL] = WorkflowStepType.APPROVAL

    reason: str = Field(
        min_length=1,
        max_length=MAX_REASON_LENGTH,
        description="What the approver is being asked, in their own terms. "
        "Literal text: never a reference, because an approval prompt assembled "
        "from business data is a way to put whatever a record contains in front "
        "of somebody as if the platform were saying it.",
    )
    next: StepId | None = Field(default=None, description="Where approval leads.")
    on_reject: StepId | None = Field(
        default=None,
        description="Where a refusal leads. Absent means the run fails, which "
        "is the safe default: a process nobody designed a 'no' path for should "
        "stop rather than quietly carry on.",
    )

    @field_validator("reason")
    @classmethod
    def _reason_is_literal(cls, value: str) -> str:
        if looks_like_reference(value):
            raise ValueError("An approval reason must be literal text, not a reference.")
        return value

    @property
    def transitions(self) -> tuple[str, ...]:
        return tuple(target for target in (self.next, self.on_reject) if target)


WorkflowStepDefinition = Annotated[
    ToolCallStep | AgentStep | ConditionStep | ApprovalStep,
    Field(discriminator="type"),
]
"""A step, told apart by ``type``."""


class WorkflowDefinition(BaseModel):
    """The whole document.

    Structural rules only - unique ids, an entry that exists, transitions that
    point somewhere real. Whether the tools and agents it names exist, whether
    every step is reachable, and whether it fits the deployment's limits are
    :mod:`app.workflows.validation`'s job, because those answers depend on what
    is installed and configured rather than on the document alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: StepId = Field(description="The step a run starts at.")
    steps: list[WorkflowStepDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique_and_entry_exists(self) -> Self:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"Two steps share the id {step.id!r}.")
            seen.add(step.id)

        if self.entry not in seen:
            raise ValueError(f"The entry step {self.entry!r} is not in the workflow.")

        for step in self.steps:
            for target in step.transitions:
                if target not in seen:
                    raise ValueError(f"Step {step.id!r} goes to {target!r}, which does not exist.")

        return self

    @property
    def by_id(self) -> dict[str, WorkflowStepDefinition]:
        return {step.id: step for step in self.steps}

    def step(self, step_id: str) -> WorkflowStepDefinition:
        """One step by id.

        Raises:
            KeyError: No such step. Only reachable if a stored definition and a
                stored run have drifted apart, which versioning prevents.
        """
        return self.by_id[step_id]
