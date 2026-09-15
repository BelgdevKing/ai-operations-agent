"""How a workflow step reads what an earlier step produced.

    $.input.shipment_reference
    $.steps.get_shipment.output.shipment.status

That is the whole language. It has no operators, no function calls, no
arithmetic and no way to reach anything the engine did not put in front of it -
because a workflow definition is **data written by a user of the platform**, and
the moment it can express computation it is a way to run code on the server.

So there is no expression evaluator here, and there is deliberately no place one
could be added without rewriting this module. A reference is parsed into a fixed
shape - which root, which step, which path - and resolution is a walk down plain
dictionaries and lists. Anything it cannot find is an error, never a guess.

What a reference cannot reach, by construction rather than by filtering:

* the database session, the settings, the tool registry, the agent runtime;
* credentials, tokens, or anything else the process holds;
* ``organization_id``, ``user_id``, ``request_id`` or ``run_id`` - those are
  execution context, put there by the server, and are not in the context object
  a reference is resolved against at all;
* any Python attribute: the walk uses ``dict`` and ``list`` indexing only, so a
  path segment like ``__class__`` finds nothing and fails.
"""

from __future__ import annotations

import enum
import re
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# What marks a string as a reference rather than a literal. A value that does
# not start with this is used exactly as written, which is what makes a workflow
# able to pass ordinary strings without escaping them.
REFERENCE_PREFIX = "$."

# Roots a reference may start from. There are two, and adding a third is a
# deliberate change to this file rather than something a definition can do.
ROOT_INPUT = "input"
ROOT_STEPS = "steps"

# A step's own results live under this key, so `$.steps.x.output.y` reads
# naturally and there is one obvious place for anything else a step might
# expose later.
OUTPUT_KEY = "output"

# Step ids select a node in the graph, so they are identifiers rather than free
# text - the same shape a tool name has, for the same reason.
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

# A path segment addresses one key of a mapping or one index of a list. Bounded
# and conservative: no dots, no brackets, no quoting rules to get wrong.
SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# How deep a reference may go. A pathological path is not dangerous - the walk
# is iterative - but it is a sign of a definition nobody can read, and a limit
# is cheaper than finding out.
MAX_REFERENCE_DEPTH = 12


class ReferenceRoot(enum.StrEnum):
    """Where a reference starts."""

    INPUT = ROOT_INPUT
    STEPS = ROOT_STEPS


class Reference(BaseModel):
    """A parsed reference. Frozen, because it is part of a definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: ReferenceRoot
    step_id: str | None = Field(
        default=None, description="Which step, for a reference into step output."
    )
    path: tuple[str, ...] = Field(default=(), description="Keys and indices to walk.")

    @model_validator(mode="after")
    def _step_reference_names_a_step(self) -> Self:
        if self.root is ReferenceRoot.STEPS and not self.step_id:
            raise ValueError("A step reference must name a step.")
        if self.root is ReferenceRoot.INPUT and self.step_id:
            raise ValueError("An input reference cannot name a step.")
        return self

    def render(self) -> str:
        """The reference as it was written, for error messages and round trips."""
        if self.root is ReferenceRoot.INPUT:
            return REFERENCE_PREFIX + ".".join([ROOT_INPUT, *self.path])
        return REFERENCE_PREFIX + ".".join([ROOT_STEPS, str(self.step_id), OUTPUT_KEY, *self.path])


class ReferenceError(ValueError):
    """A reference could not be parsed, or could not be resolved.

    A ``ValueError`` so that Pydantic reports it as a validation failure when a
    definition is being parsed, and so the engine can catch one kind of thing
    when resolving at run time.
    """


def looks_like_reference(value: object) -> bool:
    """Whether *value* is meant as a reference rather than a literal."""
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIX)


def parse_reference(text: str) -> Reference:
    """Turn ``$.input.x`` or ``$.steps.a.output.b`` into a :class:`Reference`.

    Strict on purpose. Every rejected shape below is a shape somebody could
    otherwise rely on by accident, and a reference language people discover the
    edges of by experiment is one nobody can reason about later.

    Raises:
        ReferenceError: Anything that is not one of the two supported forms.
    """
    if not looks_like_reference(text):
        raise ReferenceError(f"A reference must start with {REFERENCE_PREFIX!r}.")

    parts = text[len(REFERENCE_PREFIX) :].split(".")
    if not parts or not parts[0]:
        raise ReferenceError("A reference must name a root.")

    root, rest = parts[0], parts[1:]

    if root == ROOT_INPUT:
        return Reference(root=ReferenceRoot.INPUT, path=_checked_path(rest, text))

    if root == ROOT_STEPS:
        if len(rest) < 2:
            raise ReferenceError(
                f"{text!r} is incomplete: a step reference reads "
                f"{REFERENCE_PREFIX}steps.<step>.{OUTPUT_KEY}[.<path>]."
            )
        step_id, marker, path = rest[0], rest[1], rest[2:]

        if not re.match(STEP_ID_PATTERN, step_id):
            raise ReferenceError(f"{step_id!r} is not a step id.")
        if marker != OUTPUT_KEY:
            raise ReferenceError(
                f"{text!r} reads {marker!r} where {OUTPUT_KEY!r} was expected: a step "
                f"exposes only its output."
            )

        return Reference(root=ReferenceRoot.STEPS, step_id=step_id, path=_checked_path(path, text))

    raise ReferenceError(
        f"{root!r} is not something a reference can start from; use "
        f"{ROOT_INPUT!r} or {ROOT_STEPS!r}."
    )


def _checked_path(segments: list[str], text: str) -> tuple[str, ...]:
    if len(segments) > MAX_REFERENCE_DEPTH:
        raise ReferenceError(
            f"{text!r} is {len(segments)} levels deep; the limit is {MAX_REFERENCE_DEPTH}."
        )
    for segment in segments:
        if not SEGMENT_PATTERN.match(segment):
            raise ReferenceError(f"{segment!r} is not a usable path segment in {text!r}.")
    return tuple(segments)


# -- Resolution ---------------------------------------------------------------


MISSING = object()
"""Returned when a reference points at nothing. Distinct from a stored null."""


def resolve(reference: Reference, data: dict[str, Any]) -> object:
    """Read what *reference* points at, or return :data:`MISSING`.

    *data* is the workflow context as a plain dictionary - input, and the output
    each finished step produced. Nothing else is in it, which is why this
    function needs no allow-list: there is nothing to exclude.

    The walk is by ``dict`` key and ``list`` index only. Attribute access is
    never attempted, so a path naming ``__class__`` or ``__globals__`` resolves
    to :data:`MISSING` like any other key that is not there.
    """
    if reference.root is ReferenceRoot.INPUT:
        current: object = data.get(ROOT_INPUT, {})
    else:
        step = (data.get(ROOT_STEPS) or {}).get(reference.step_id)
        if step is None:
            return MISSING
        current = step.get(OUTPUT_KEY, MISSING)
        if current is MISSING:
            return MISSING

    for segment in reference.path:
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
            continue

        if isinstance(current, list):
            index = _as_index(segment)
            if index is None or not -len(current) <= index < len(current):
                return MISSING
            current = current[index]
            continue

        # A scalar with path left to walk. Not an error worth raising: a
        # condition asking "does this exist?" about a field that turned out to
        # be a string should answer no, not fail the workflow.
        return MISSING

    return current


def _as_index(segment: str) -> int | None:
    try:
        return int(segment)
    except ValueError:
        return None
