"""What an approver is told about a proposed action.

An approval queue has a hard problem in it: a person cannot decide well without
knowing *which* record is about to be changed, and the values that say which
record are the tenant's business data sitting inside a payload a language model
wrote. Publishing the payload solves the first problem by creating a worse one.

So the tool author decides, once, in code::

    approval_summary = ApprovalSummary(
        action="Cancel shipment",
        subject_field="shipment_reference",
        detail_fields=("reason",),
    )

and nothing else from the arguments is ever disclosed. The allow-list is applied
at the moment the approval is requested, and only its output is stored - so the
approval record does not contain the payload with some fields hidden, it
contains a projection that never had the other fields in it.

Three properties are worth stating plainly, because each one is a thing this
module refuses to do:

**No templates.** ``action`` is a fixed phrase written by a developer. There is
no expression language, no placeholder syntax, no ``eval``, no ``format`` over
caller-supplied text. The headline is built by concatenation and nothing else.

**No structures.** A field whose value is a dict or a list is skipped, not
serialised. Rendering a nested object would be republishing the payload one
level down, which is the thing this exists to prevent.

**No unbounded text.** Values are stripped of control characters, collapsed to
single spaces and truncated. What reaches a screen is plain text of known
length, whoever wrote it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# An argument name, matching the shape a Pydantic field can actually have.
FIELD_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

FieldName = Annotated[str, Field(pattern=FIELD_NAME_PATTERN)]

MAX_DETAIL_FIELDS = 8
"""How many fields one tool may put in front of an approver.

A summary is a sentence and a short list, not a record viewer. A tool that wants
to show more than this has stopped summarising and started exporting.
"""

MAX_VALUE_CHARACTERS = 120
"""Longest a single rendered value may be. Longer is truncated, never dropped."""

MAX_HEADLINE_CHARACTERS = 200
"""Fits ``approvals.summary``. Also about as much as anybody reads in a queue."""

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_RUNS_OF_WHITESPACE = re.compile(r"\s+")

_ELLIPSIS = "..."


class ApprovalSummary(BaseModel):
    """Which of a tool's arguments an approver may be shown.

    Declared on :class:`app.tools.models.ToolMetadata`, checked against the
    tool's input schema when the class is defined, and applied when an approval
    is requested.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str = Field(
        min_length=1,
        max_length=80,
        description="What the tool does, as a fixed phrase a developer wrote: "
        "'Cancel shipment', 'Send customer notification'. Never interpolated, "
        "never derived from arguments, never written by a model.",
    )

    subject_field: FieldName | None = Field(
        default=None,
        description="The one argument naming the record being acted on. Its "
        "value is appended to the action to make the headline, which is why "
        "there is exactly one rather than a list.",
    )

    detail_fields: tuple[FieldName, ...] = Field(
        default=(),
        description="Further arguments safe to show. Order is preserved; the "
        "subject appears first regardless.",
    )

    @model_validator(mode="after")
    def _bounded_and_distinct(self) -> Self:
        if len(self.detail_fields) > MAX_DETAIL_FIELDS:
            raise ValueError(f"At most {MAX_DETAIL_FIELDS} detail fields may be declared.")

        named = self.named_fields
        if len(named) != len(set(named)):
            raise ValueError("A field may be declared once. Listing it twice shows it twice.")

        return self

    @property
    def named_fields(self) -> tuple[str, ...]:
        """Every argument this summary discloses, subject first."""
        if self.subject_field is None:
            return self.detail_fields
        return (self.subject_field, *self.detail_fields)


@dataclass(frozen=True)
class SummaryField:
    """One labelled value, already safe to render."""

    label: str
    value: str

    def as_dict(self) -> dict[str, str]:
        """The stored shape. Two known keys, both strings."""
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class ActionSummary:
    """What is being proposed, in terms an approver can act on."""

    headline: str
    fields: tuple[SummaryField, ...] = ()

    def as_dicts(self) -> list[dict[str, str]]:
        return [field.as_dict() for field in self.fields]


def build_summary(spec: ApprovalSummary, arguments: Mapping[str, Any]) -> ActionSummary:
    """Project the arguments onto what the tool declared safe to show.

    An argument that is absent, null, or not a scalar is skipped rather than
    rendered - a missing subject leaves the action standing on its own, which
    reads as "Cancel shipment" and is still true.

    Args:
        spec: The tool's declaration. Written in code; trusted.
        arguments: What the tool was asked. Written by a model; untrusted, and
            only ever read through *spec*.
    """
    fields: list[SummaryField] = []

    for name in spec.named_fields:
        rendered = _render(arguments.get(name))
        if rendered is None:
            continue
        fields.append(SummaryField(label=_humanise(name), value=rendered))

    headline = spec.action
    if spec.subject_field is not None:
        subject = _render(arguments.get(spec.subject_field))
        if subject is not None:
            headline = f"{spec.action} {subject}"

    return ActionSummary(headline=_bound(headline, MAX_HEADLINE_CHARACTERS), fields=tuple(fields))


def _render(value: object) -> str | None:
    """One argument as safe plain text, or nothing at all.

    ``bool`` is checked before ``int`` on purpose: in Python a bool *is* an int,
    and an approver reading "Force: 1" learns less than one reading "Force: yes".
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return "yes" if value else "no"

    if isinstance(value, str):
        text = value
    elif isinstance(value, int | float):
        text = str(value)
    else:
        # A dict, a list, a model, an arbitrary object. Deliberately unrendered:
        # summarising a structure means publishing it, one level down.
        return None

    cleaned = _clean(text)
    return cleaned or None


def plain_text(text: str, *, limit: int) -> str:
    """Plain, single-spaced, printable, and no longer than *limit*.

    Public because it is not really about summaries: it is what this codebase
    does to any untrusted string on its way to a reviewer's screen, and the
    decision reason a person types into the approval panel gets the same
    treatment for the same reasons. Control characters go because a terminal or
    a log reader will act on some of them; whitespace collapses because a
    thousand newlines is a layout attack on a queue; the length is bounded
    because the column is.

    There is deliberately no HTML escaping here. The frontend renders text as
    text, which is where escaping belongs - doing it at the storage boundary
    would mean storing ``&amp;`` and hoping every future reader decodes it.
    """
    without_controls = _CONTROL_CHARACTERS.sub(" ", text)
    collapsed = _RUNS_OF_WHITESPACE.sub(" ", without_controls).strip()
    return _bound(collapsed, limit)


def _clean(text: str) -> str:
    return plain_text(text, limit=MAX_VALUE_CHARACTERS)


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _humanise(field_name: str) -> str:
    """``shipment_reference`` -> ``Shipment reference``.

    A label, not a translation. The field names are English identifiers chosen
    by the tool author, and treating them as words is closer to right than
    printing the identifier.
    """
    words = field_name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]
