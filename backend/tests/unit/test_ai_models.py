"""Provider-independent AI types.

Pure unit tests: no database, no network, no provider SDK.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.ai.models import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_TEMPERATURE,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMStructuredResponse,
    LLMUsage,
)


def a_request(**overrides: object) -> LLMRequest:
    payload: dict[str, object] = {
        "messages": [LLMMessage.user("Summarise last week's refunds.")],
        "model": "some-model-id",
    }
    payload.update(overrides)
    return LLMRequest(**payload)  # type: ignore[arg-type]


# -- LLMRole ------------------------------------------------------------------


def test_roles_are_system_user_assistant() -> None:
    assert {role.value for role in LLMRole} == {"system", "user", "assistant"}


def test_roles_compare_equal_to_their_string() -> None:
    """StrEnum, so callers may pass either the member or the plain value."""
    assert LLMRole.USER == "user"


def test_tool_is_not_a_role_yet() -> None:
    """Tool results are out of scope until tool execution exists; the database
    enum carries 'tool' but this abstraction deliberately does not."""
    assert not hasattr(LLMRole, "TOOL")


# -- LLMMessage ---------------------------------------------------------------


@pytest.mark.parametrize("role", list(LLMRole))
def test_a_message_accepts_every_role(role: LLMRole) -> None:
    message = LLMMessage(role=role, content="text")

    assert message.role is role
    assert message.content == "text"


def test_a_role_may_be_given_as_a_string() -> None:
    assert LLMMessage(role="assistant", content="text").role is LLMRole.ASSISTANT  # type: ignore[arg-type]


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LLMMessage(role="tool", content="text")  # type: ignore[arg-type]


def test_empty_content_is_rejected() -> None:
    """Every provider treats it as an error; failing here gives a better
    message than the provider's."""
    with pytest.raises(ValidationError):
        LLMMessage(role=LLMRole.USER, content="")


def test_convenience_constructors_set_the_right_role() -> None:
    assert LLMMessage.system("s").role is LLMRole.SYSTEM
    assert LLMMessage.user("u").role is LLMRole.USER
    assert LLMMessage.assistant("a").role is LLMRole.ASSISTANT


def test_a_message_is_immutable() -> None:
    """Requests are passed down through the gateway to an adapter; a layer
    that mutated one would change what a caller believes it sent."""
    message = LLMMessage.user("text")

    with pytest.raises(ValidationError):
        message.content = "something else"  # type: ignore[misc]


# -- LLMRequest ---------------------------------------------------------------


def test_a_request_needs_only_messages_and_a_model() -> None:
    request = a_request()

    assert request.temperature == DEFAULT_TEMPERATURE
    assert request.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS


def test_the_model_has_no_default() -> None:
    """A default would name a real model and make this module
    provider-dependent."""
    with pytest.raises(ValidationError):
        LLMRequest(messages=[LLMMessage.user("hello")])  # type: ignore[call-arg]


def test_an_empty_model_is_rejected() -> None:
    with pytest.raises(ValidationError):
        a_request(model="")


def test_a_request_needs_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        a_request(messages=[])


@pytest.mark.parametrize("temperature", [-0.1, MAX_TEMPERATURE + 0.1, 100.0])
def test_temperature_outside_the_supported_range_is_rejected(temperature: float) -> None:
    with pytest.raises(ValidationError):
        a_request(temperature=temperature)


@pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0, MAX_TEMPERATURE])
def test_temperature_inside_the_range_is_accepted(temperature: float) -> None:
    assert a_request(temperature=temperature).temperature == temperature


@pytest.mark.parametrize("tokens", [0, -1])
def test_max_output_tokens_must_be_positive(tokens: int) -> None:
    with pytest.raises(ValidationError):
        a_request(max_output_tokens=tokens)


def test_a_request_is_immutable() -> None:
    request = a_request()

    with pytest.raises(ValidationError):
        request.model = "another-model"  # type: ignore[misc]


def test_system_messages_are_separable_from_the_conversation() -> None:
    """Some providers take the system prompt as a top-level parameter rather
    than a message, so adapters need the split."""
    request = a_request(
        messages=[
            LLMMessage.system("Be concise."),
            LLMMessage.user("Question?"),
            LLMMessage.assistant("Answer."),
            LLMMessage.system("Also be polite."),
        ]
    )

    assert [m.content for m in request.system_messages] == ["Be concise.", "Also be polite."]
    assert [m.role for m in request.conversation_messages] == [LLMRole.USER, LLMRole.ASSISTANT]


def test_the_split_preserves_order() -> None:
    request = a_request(
        messages=[LLMMessage.user("one"), LLMMessage.assistant("two"), LLMMessage.user("three")]
    )

    assert [m.content for m in request.conversation_messages] == ["one", "two", "three"]


def test_a_request_with_no_system_message_splits_cleanly() -> None:
    request = a_request()

    assert request.system_messages == []
    assert len(request.conversation_messages) == 1


# -- LLMUsage -----------------------------------------------------------------


def test_usage_defaults_to_zero() -> None:
    """A provider that reports nothing still yields a usable record."""
    usage = LLMUsage()

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_tokens == 0


def test_total_tokens_is_the_sum() -> None:
    assert LLMUsage(input_tokens=100, output_tokens=25).total_tokens == 125


def test_total_tokens_cannot_be_set_independently() -> None:
    """Derived, so it can never disagree with its parts."""
    usage = LLMUsage(input_tokens=10, output_tokens=5, total_tokens=999)  # type: ignore[call-arg]

    assert usage.total_tokens == 15


def test_total_tokens_is_serialised() -> None:
    assert LLMUsage(input_tokens=3, output_tokens=4).model_dump() == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }


@pytest.mark.parametrize(("field", "value"), [("input_tokens", -1), ("output_tokens", -1)])
def test_negative_token_counts_are_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        LLMUsage(**{field: value})  # type: ignore[arg-type]


# -- LLMResponse --------------------------------------------------------------


def a_response(**overrides: object) -> LLMResponse:
    payload: dict[str, object] = {
        "content": "Four refunds are pending.",
        "provider": "some-provider",
        "model": "some-model-id",
        "latency_ms": 421.5,
    }
    payload.update(overrides)
    return LLMResponse(**payload)  # type: ignore[arg-type]


def test_a_response_carries_provider_model_and_latency() -> None:
    response = a_response()

    assert response.provider == "some-provider"
    assert response.model == "some-model-id"
    assert response.latency_ms == 421.5


def test_usage_defaults_so_a_response_is_always_countable() -> None:
    assert a_response().usage.total_tokens == 0


def test_request_id_is_optional() -> None:
    """Not every provider returns one."""
    assert a_response().request_id is None
    assert a_response(request_id="req_123").request_id == "req_123"


def test_empty_content_is_allowed_in_a_response() -> None:
    """Unlike a request message: a model genuinely can return nothing, and
    rejecting it here would turn a real outcome into a crash."""
    assert a_response(content="").content == ""


@pytest.mark.parametrize("field", ["provider", "model"])
def test_provenance_fields_cannot_be_empty(field: str) -> None:
    """Every response must say what produced it, for cost attribution."""
    with pytest.raises(ValidationError):
        a_response(**{field: ""})


def test_negative_latency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        a_response(latency_ms=-1)


def test_a_response_is_immutable() -> None:
    with pytest.raises(ValidationError):
        a_response().content = "rewritten"  # type: ignore[misc]


# -- LLMStructuredResponse ----------------------------------------------------


class RefundSummary(BaseModel):
    pending: int
    currency: str


def test_a_structured_response_carries_the_parsed_value_and_the_call() -> None:
    structured = LLMStructuredResponse[RefundSummary](
        data=RefundSummary(pending=4, currency="GBP"),
        response=a_response(),
    )

    assert structured.data.pending == 4
    assert structured.response.provider == "some-provider"


def test_a_structured_response_validates_its_payload() -> None:
    with pytest.raises(ValidationError):
        LLMStructuredResponse[RefundSummary](
            data={"pending": "not a number", "currency": "GBP"},  # type: ignore[arg-type]
            response=a_response(),
        )


def test_usage_survives_structured_parsing() -> None:
    """The parsed value is the point, but the call still has to be billable."""
    structured = LLMStructuredResponse[RefundSummary](
        data=RefundSummary(pending=1, currency="GBP"),
        response=a_response(usage=LLMUsage(input_tokens=50, output_tokens=10)),
    )

    assert structured.response.usage.total_tokens == 60
