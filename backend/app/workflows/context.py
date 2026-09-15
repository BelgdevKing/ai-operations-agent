"""The data a workflow carries between its steps.

    {
      "input": { ... what the run was started with ... },
      "steps": { "get_shipment": {"output": { ... }}, ... }
    }

That dictionary is the whole context. It is built by the engine from the run's
input and the output each finished step produced, and it is what a reference is
resolved against - so the question "what can a workflow definition read?" has a
literal answer: this object, and nothing else.

What is deliberately **not** in it, and therefore cannot be reached however a
definition is written:

* the database session, the settings, the registries, the executor;
* ``organization_id``, ``user_id``, ``request_id``, ``run_id`` - execution
  context established by the server from a verified membership, which a
  workflow is not permitted to read and could not usefully change;
* credentials, tokens or anything else the process holds.

Interpolation is substitution, not evaluation. A value either *is* a reference -
a string starting with ``$.`` - and is replaced by what it points at, or it is
used exactly as written. There is no concatenation, no formatting and no
expression, because a workflow definition is a document somebody uploaded.
"""

from __future__ import annotations

import json
from typing import Any

from app.workflows.definition import (
    COLLECTION_OPERATORS,
    ORDERING_OPERATORS,
    UNARY_OPERATORS,
    Condition,
    ConditionOperator,
)
from app.workflows.exceptions import WorkflowReferenceError
from app.workflows.references import (
    MISSING,
    OUTPUT_KEY,
    ROOT_INPUT,
    ROOT_STEPS,
    Reference,
    looks_like_reference,
    parse_reference,
    resolve,
)


class WorkflowContext:
    """What the run knows so far.

    Mutable only through :meth:`record`, which the engine calls once per
    completed step. Resolution never writes, so a reference cannot have a side
    effect - which is most of why references are safe to allow at all.
    """

    def __init__(self, input_data: dict[str, Any] | None = None) -> None:
        self._input: dict[str, Any] = dict(input_data or {})
        self._steps: dict[str, dict[str, Any]] = {}

    @classmethod
    def rebuild(
        cls, input_data: dict[str, Any] | None, outputs: dict[str, object]
    ) -> WorkflowContext:
        """The context of a run in progress, from what is stored.

        Used when a run resumes after an approval: the request that started it
        is long gone, and this is how the next one knows what the earlier steps
        produced without re-running them.
        """
        context = cls(input_data)
        for step_id, output in outputs.items():
            context.record(step_id, output)
        return context

    def record(self, step_id: str, output: object) -> None:
        """Note what a step produced."""
        self._steps[step_id] = {OUTPUT_KEY: output}

    def output_of(self, step_id: str) -> object:
        return self._steps.get(step_id, {}).get(OUTPUT_KEY)

    @property
    def data(self) -> dict[str, Any]:
        """The context as the reference resolver sees it."""
        return {ROOT_INPUT: self._input, ROOT_STEPS: self._steps}

    # -- Reading ---------------------------------------------------------------

    def resolve(self, reference: Reference) -> object:
        """What *reference* points at.

        Raises:
            WorkflowReferenceError: It points at nothing. Refused rather than
                substituted with null, because a tool called with a silently
                missing argument is a tool called wrongly, and a condition
                comparing against an accidental null is a branch taken for the
                wrong reason. ``exists`` is the operator for asking.
        """
        value = resolve(reference, self.data)
        if value is MISSING:
            raise WorkflowReferenceError(
                f"{reference.render()} is not available at this point in the workflow."
            )
        return value

    def interpolate(self, arguments: dict[str, Any]) -> dict[str, object]:
        """Substitute references in a step's arguments.

        One level deep, over the argument values a definition declares. Nested
        structures are passed through untouched: a workflow that needs to build
        a nested payload from several references is a workflow that wants a
        template language, and a template language is the thing this design
        exists to not have.
        """
        return {
            name: self.resolve(parse_reference(value)) if looks_like_reference(value) else value
            for name, value in arguments.items()
        }


# -- Conditions ---------------------------------------------------------------


def evaluate(condition: Condition, context: WorkflowContext) -> bool:
    """Decide one branch.

    Every operator is a comparison between a value the context already holds and
    a constant the definition already carries. Nothing here parses, compiles or
    calls anything.
    """
    operator = condition.operator

    if operator in UNARY_OPERATORS:
        present = resolve(condition.reference, context.data) is not MISSING
        return present if operator is ConditionOperator.EXISTS else not present

    # Every other operator needs the value, so an absent one is a real problem
    # rather than a false comparison.
    actual = context.resolve(condition.reference)

    if operator is ConditionOperator.EQUALS:
        return _equal(actual, condition.value)
    if operator is ConditionOperator.NOT_EQUALS:
        return not _equal(actual, condition.value)

    if operator in COLLECTION_OPERATORS:
        values = condition.value if isinstance(condition.value, list) else []
        contained = any(_equal(actual, candidate) for candidate in values)
        return contained if operator is ConditionOperator.IN else not contained

    if operator in ORDERING_OPERATORS:
        return _ordered(operator, actual, condition.value)

    # Unreachable: the operator set is closed and every member is handled above.
    raise WorkflowReferenceError(f"{operator!r} cannot be evaluated.")


def _equal(actual: object, expected: object) -> bool:
    """Equality, without Python's booleans-are-integers surprise.

    ``True == 1`` is true in Python, which would make a condition comparing a
    status flag against ``1`` quietly match. A business rule should not inherit
    that.
    """
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return bool(actual == expected)


def _ordered(operator: ConditionOperator, actual: object, expected: object) -> bool:
    """A numeric comparison, or False if the value is not a number.

    Not an error: "is the outstanding amount greater than 100" asked about a
    field that turned out to be a string is a condition that is not satisfied,
    and failing the whole workflow over it would be a worse answer than taking
    the false branch.

    Booleans are excluded explicitly - ``True > 0`` is true in Python and means
    nothing in a business rule.
    """
    if isinstance(actual, bool) or not isinstance(actual, int | float):
        return False
    if not isinstance(expected, int | float):
        return False

    if operator is ConditionOperator.GREATER_THAN:
        return actual > expected
    if operator is ConditionOperator.GREATER_THAN_OR_EQUAL:
        return actual >= expected
    if operator is ConditionOperator.LESS_THAN:
        return actual < expected
    return actual <= expected


# -- Payload limits -----------------------------------------------------------


def encoded_size(payload: object) -> int:
    """How many bytes a payload occupies once serialised.

    Measured on the JSON the database will actually store, rather than
    estimated, because the limit exists to bound what goes into a column.
    """
    return len(json.dumps(payload, default=str).encode("utf-8"))


def depth_of(payload: object, *, _level: int = 1) -> int:
    """How deeply nested a payload is.

    Iterative would be tidier; recursion is bounded here because the caller
    checks the size first and a payload small enough to pass that cannot nest
    deeply enough to matter.
    """
    if isinstance(payload, dict):
        return max(
            (depth_of(value, _level=_level + 1) for value in payload.values()), default=_level
        )
    if isinstance(payload, list):
        return max((depth_of(value, _level=_level + 1) for value in payload), default=_level)
    return _level
