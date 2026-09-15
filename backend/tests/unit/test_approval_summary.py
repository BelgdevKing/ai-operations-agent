"""What an approver is told about a proposed action.

The whole of this module is one question asked repeatedly: given arguments a
model wrote, what reaches a human reviewer's screen? The answer has to be
*exactly what the tool declared* - no more, because the rest is business data
nobody chose to publish, and no less, because a reviewer who cannot see which
record is affected cannot review anything.

So the tests come in two halves. The first is that the declared fields arrive
intact. The second, and the longer one, is the list of things that do not: a
field the tool did not name, a nested structure, a control character, a
thousand-character string, a template somebody hoped would be interpolated.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError

from app.services.approvals import MAX_DECISION_REASON_CHARACTERS, clean_decision_reason
from app.tools.base import Tool
from app.tools.exceptions import ToolRegistrationError
from app.tools.models import ToolExecutionContext, ToolMetadata, ToolSafety
from app.tools.summary import (
    MAX_DETAIL_FIELDS,
    MAX_HEADLINE_CHARACTERS,
    MAX_VALUE_CHARACTERS,
    ActionSummary,
    ApprovalSummary,
    build_summary,
    plain_text,
)

CANCEL = ApprovalSummary(
    action="Cancel shipment",
    subject_field="shipment_reference",
    detail_fields=("reason",),
)


# -- What arrives -------------------------------------------------------------


def test_the_headline_is_the_action_and_the_subject() -> None:
    """The example from the specification, exactly."""
    summary = build_summary(CANCEL, {"shipment_reference": "ABC123", "reason": "Customer asked."})

    assert summary.headline == "Cancel shipment ABC123"


def test_the_fields_are_labelled_and_in_declared_order() -> None:
    summary = build_summary(CANCEL, {"shipment_reference": "ABC123", "reason": "Customer asked."})

    assert [(field.label, field.value) for field in summary.fields] == [
        ("Shipment reference", "ABC123"),
        ("Reason", "Customer asked."),
    ]


def test_the_subject_comes_first_however_it_was_listed() -> None:
    spec = ApprovalSummary(
        action="Send notice", subject_field="customer", detail_fields=("channel", "template")
    )

    summary = build_summary(spec, {"channel": "email", "template": "late", "customer": "CUST-1"})

    assert [field.label for field in summary.fields] == ["Customer", "Channel", "Template"]


def test_a_stored_summary_is_two_known_string_keys() -> None:
    """The shape ``approvals.summary_fields`` holds. Nothing else gets in."""
    summary = build_summary(CANCEL, {"shipment_reference": "ABC123", "reason": "Because."})

    assert summary.as_dicts() == [
        {"label": "Shipment reference", "value": "ABC123"},
        {"label": "Reason", "value": "Because."},
    ]


def test_numbers_and_booleans_are_rendered_for_a_person() -> None:
    spec = ApprovalSummary(
        action="Adjust charge", subject_field="amount", detail_fields=("confirmed",)
    )

    summary = build_summary(spec, {"amount": 42, "confirmed": True})

    assert summary.headline == "Adjust charge 42"
    assert summary.fields[1].value == "yes", "'1' would tell a reviewer less"


# -- What does not ------------------------------------------------------------


def test_an_undeclared_argument_is_not_disclosed() -> None:
    """The allow-list is the security boundary, and this is it working."""
    summary = build_summary(
        CANCEL,
        {
            "shipment_reference": "ABC123",
            "reason": "Customer asked.",
            "internal_note": "Escalated by finance; customer is disputing 4 invoices.",
            "api_key": "sk-not-a-real-key",
        },
    )

    rendered = summary.headline + str(summary.as_dicts())
    assert "internal_note" not in rendered
    assert "Escalated" not in rendered
    assert "sk-not-a-real-key" not in rendered


def test_a_nested_structure_is_skipped_rather_than_serialised() -> None:
    """Rendering a dict would be republishing the payload one level down."""
    spec = ApprovalSummary(
        action="Update record", subject_field="target", detail_fields=("detail",)
    )

    summary = build_summary(spec, {"target": "REC-1", "detail": {"secret": "value"}})

    assert summary.headline == "Update record REC-1"
    assert [field.label for field in summary.fields] == ["Target"]


def test_a_list_is_skipped_too() -> None:
    spec = ApprovalSummary(action="Notify", detail_fields=("recipients",))

    summary = build_summary(spec, {"recipients": ["a@example.com", "b@example.com"]})

    assert summary.fields == ()
    assert "example.com" not in summary.headline


def test_an_absent_subject_leaves_the_action_standing_alone() -> None:
    summary = build_summary(CANCEL, {"reason": "Customer asked."})

    assert summary.headline == "Cancel shipment"
    assert [field.label for field in summary.fields] == ["Reason"]


def test_a_null_value_is_absent_rather_than_the_word_none() -> None:
    summary = build_summary(CANCEL, {"shipment_reference": None, "reason": None})

    assert summary.headline == "Cancel shipment"
    assert summary.fields == ()


def test_an_empty_string_is_absent_too() -> None:
    summary = build_summary(CANCEL, {"shipment_reference": "   ", "reason": ""})

    assert summary.headline == "Cancel shipment"
    assert summary.fields == ()


def test_control_characters_are_stripped() -> None:
    """A reviewer's screen is not a terminal, and a log reader is."""
    summary = build_summary(CANCEL, {"shipment_reference": "ABC\x1b[31m123\x00\x07"})

    assert "\x1b" not in summary.headline
    assert "\x00" not in summary.headline
    assert summary.headline == "Cancel shipment ABC [31m123"


def test_newlines_collapse_so_one_value_stays_one_line() -> None:
    summary = build_summary(CANCEL, {"reason": "Line one.\n\n\nLine two."})

    assert summary.fields[0].value == "Line one. Line two."


def test_a_long_value_is_truncated_not_dropped() -> None:
    summary = build_summary(CANCEL, {"shipment_reference": "X" * 5_000})

    value = summary.fields[0].value
    assert len(value) == MAX_VALUE_CHARACTERS
    assert value.endswith("...")


def test_the_headline_is_bounded_by_the_column_it_is_stored_in() -> None:
    spec = ApprovalSummary(action="A" * 80, subject_field="subject")

    summary = build_summary(spec, {"subject": "B" * 500})

    assert len(summary.headline) <= MAX_HEADLINE_CHARACTERS


def test_a_template_in_the_arguments_is_text_and_nothing_else() -> None:
    """There is no interpolation here to exploit, and this is what that means."""
    hostile = "{action} {0} ${{x}} <script>alert(1)</script> {shipment_reference}"

    summary = build_summary(CANCEL, {"shipment_reference": hostile})

    assert summary.headline == f"Cancel shipment {hostile}"


# -- Declaring one ------------------------------------------------------------


def test_a_field_may_not_be_declared_twice() -> None:
    with pytest.raises(ValidationError):
        ApprovalSummary(action="Do it", subject_field="x", detail_fields=("x",))


def test_the_number_of_detail_fields_is_bounded() -> None:
    too_many = tuple(f"field_{index}" for index in range(MAX_DETAIL_FIELDS + 1))

    with pytest.raises(ValidationError):
        ApprovalSummary(action="Do it", detail_fields=too_many)


def test_a_field_name_must_look_like_a_field_name() -> None:
    with pytest.raises(ValidationError):
        ApprovalSummary(action="Do it", subject_field="not a field")


def test_the_action_is_required_and_bounded() -> None:
    with pytest.raises(ValidationError):
        ApprovalSummary(action="")

    with pytest.raises(ValidationError):
        ApprovalSummary(action="A" * 200)


def test_only_a_gated_tool_may_declare_a_summary() -> None:
    """A summary on an ungated tool would tell the next reader it is gated."""
    with pytest.raises(ValidationError, match="requires approval"):
        ToolMetadata(
            name="look_up",
            description="Reads a thing.",
            safety=ToolSafety.READ_ONLY,
            approval_summary=ApprovalSummary(action="Look up"),
        )


# -- Declaring one on a real tool ---------------------------------------------


class Target(BaseModel):
    reference: str = Field(default="")
    note: str = Field(default="")


class Result(BaseModel):
    done: bool = True


def test_a_summary_over_an_unknown_argument_is_refused_at_import_time() -> None:
    """A misspelt field would silently render as absent forever."""
    with pytest.raises(ToolRegistrationError, match="unknown argument"):

        class Mistyped(Tool[Target, Result]):
            metadata = ToolMetadata(
                name="mistyped",
                description="Declares a field it does not take.",
                safety=ToolSafety.DESTRUCTIVE,
                requires_approval=True,
                approval_summary=ApprovalSummary(action="Do", subject_field="referenc"),
            )
            input_model = Target
            output_model = Result

            async def execute(self, arguments: Target, context: ToolExecutionContext) -> Result:
                del arguments, context
                return Result()


def test_a_summary_over_declared_arguments_is_accepted() -> None:
    class Fine(Tool[Target, Result]):
        metadata = ToolMetadata(
            name="fine",
            description="Declares fields it takes.",
            safety=ToolSafety.DESTRUCTIVE,
            requires_approval=True,
            approval_summary=ApprovalSummary(
                action="Do", subject_field="reference", detail_fields=("note",)
            ),
        )
        input_model = Target
        output_model = Result

        async def execute(self, arguments: Target, context: ToolExecutionContext) -> Result:
            del arguments, context
            return Result()

    assert Fine.metadata.approval_summary is not None
    assert Fine.metadata.approval_summary.named_fields == ("reference", "note")


def test_a_gated_tool_needs_no_summary() -> None:
    """Declaring none is safe, and says only the tool's name. Not an error."""
    metadata = ToolMetadata(
        name="unsummarised",
        description="Gated, and says nothing about what it would do.",
        safety=ToolSafety.DESTRUCTIVE,
        requires_approval=True,
    )

    assert metadata.approval_summary is None


# -- An empty summary ---------------------------------------------------------


def test_a_headline_with_no_fields_is_representable() -> None:
    """What a workflow approval step produces: a question, with no execution."""
    summary = ActionSummary(headline="Release the customs hold")

    assert summary.as_dicts() == []


# -- The decision reason ------------------------------------------------------


def test_no_reason_stays_no_reason() -> None:
    assert clean_decision_reason(None) is None


def test_whitespace_only_becomes_no_reason_rather_than_an_empty_string() -> None:
    """One representation of "they did not say", not two."""
    assert clean_decision_reason("   \n\t  ") is None


def test_a_reason_keeps_its_words() -> None:
    assert clean_decision_reason("Approved because the customer asked.") == (
        "Approved because the customer asked."
    )


def test_a_reason_is_stripped_of_control_characters() -> None:
    cleaned = clean_decision_reason("Fine\x00\x1b[2Jby me")

    assert "\x00" not in (cleaned or "")
    assert "\x1b" not in (cleaned or "")


def test_a_reason_is_bounded_at_the_column_width() -> None:
    cleaned = clean_decision_reason("no " * 1_000)

    assert cleaned is not None
    assert len(cleaned) <= MAX_DECISION_REASON_CHARACTERS


def test_markup_in_a_reason_is_kept_as_text_not_escaped_here() -> None:
    """Escaping belongs where it is rendered.

    Storing ``&lt;`` would mean every future reader had to know to decode it,
    and the one that forgot would show the entity rather than the character.
    The frontend renders text as text; that is the whole mitigation.
    """
    cleaned = clean_decision_reason("<script>alert(1)</script>")

    assert cleaned == "<script>alert(1)</script>"


def test_the_sanitiser_is_the_one_used_everywhere() -> None:
    assert plain_text("a\x00\n\nb", limit=100) == "a b"
