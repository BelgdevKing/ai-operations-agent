"""The OpenAI adapter. Offline: the SDK client is injected."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.models import LLMMessage, LLMRequest, LLMResponse
from app.ai.providers.openai import OpenAIProvider
from tests.unit.ai_doubles import OPENAI_KEY, openai_client, openai_completion, openai_errors

MODEL = "gpt-5.5"


class RefundSummary(BaseModel):
    pending: int
    currency: str


def a_request(**overrides: Any) -> LLMRequest:
    payload: dict[str, Any] = {
        "messages": [LLMMessage.user("How many refunds are pending?")],
        "model": MODEL,
    }
    payload.update(overrides)
    return LLMRequest(**payload)


def provider(client: Any) -> OpenAIProvider:
    return OpenAIProvider(OPENAI_KEY, timeout_seconds=30.0, client=client)


def sent(client: Any) -> dict[str, Any]:
    return dict(client.chat.completions.create.await_args.kwargs)


# -- Construction -------------------------------------------------------------


def test_an_empty_key_is_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError):
        OpenAIProvider("", timeout_seconds=30.0)


def test_the_adapter_names_itself() -> None:
    assert OpenAIProvider.name == "openai"


# -- Request translation ------------------------------------------------------


async def test_the_request_is_translated_into_the_openai_format() -> None:
    client = openai_client()

    await provider(client).generate(a_request(max_output_tokens=512))

    arguments = sent(client)
    assert arguments["model"] == MODEL
    # max_tokens is rejected by current models; max_completion_tokens replaced it.
    assert arguments["max_completion_tokens"] == 512
    assert "max_tokens" not in arguments
    assert arguments["messages"] == [{"role": "user", "content": "How many refunds are pending?"}]


async def test_system_messages_stay_in_the_message_list() -> None:
    """Unlike Anthropic, OpenAI takes the system prompt as a message."""
    client = openai_client()
    request = a_request(
        messages=[
            LLMMessage.system("Be concise."),
            LLMMessage.user("Question?"),
            LLMMessage.assistant("Answer."),
        ]
    )

    await provider(client).generate(request)

    arguments = sent(client)
    assert "system" not in arguments
    assert [m["role"] for m in arguments["messages"]] == ["system", "user", "assistant"]
    assert arguments["messages"][0]["content"] == "Be concise."


async def test_message_order_is_preserved() -> None:
    client = openai_client()
    request = a_request(
        messages=[
            LLMMessage.user("one"),
            LLMMessage.assistant("two"),
            LLMMessage.user("three"),
        ]
    )

    await provider(client).generate(request)

    assert [m["content"] for m in sent(client)["messages"]] == ["one", "two", "three"]


async def test_temperature_is_sent_only_when_the_caller_asked_for_one() -> None:
    """Reasoning models reject a non-default temperature and the SDK gives no
    way to enumerate which. Omitting it unless requested keeps the common path
    working; a caller who wants one still gets a clear error if refused."""
    default = openai_client()
    await provider(default).generate(a_request())
    assert "temperature" not in sent(default)

    explicit = openai_client()
    await provider(explicit).generate(a_request(temperature=0.3))
    assert sent(explicit)["temperature"] == 0.3


# -- Response translation -----------------------------------------------------


async def test_the_response_is_translated_back() -> None:
    client = openai_client(create_result=openai_completion(text="Four."))

    response = await provider(client).generate(a_request())

    assert isinstance(response, LLMResponse)
    assert response.content == "Four."
    assert response.provider == "openai"


async def test_usage_is_translated_from_openai_field_names() -> None:
    """prompt_tokens/completion_tokens, not input_tokens/output_tokens."""
    client = openai_client(create_result=openai_completion(prompt_tokens=900, completion_tokens=25))

    response = await provider(client).generate(a_request())

    assert response.usage.input_tokens == 900
    assert response.usage.output_tokens == 25
    assert response.usage.total_tokens == 925


async def test_the_model_reported_is_the_one_that_served_the_call() -> None:
    client = openai_client(create_result=openai_completion(model="gpt-5.5-2026-04-23"))

    response = await provider(client).generate(a_request(model="gpt-5.5"))

    assert response.model == "gpt-5.5-2026-04-23"


async def test_the_request_id_is_captured() -> None:
    client = openai_client(create_result=openai_completion(request_id="req_xyz"))

    assert (await provider(client).generate(a_request())).request_id == "req_xyz"


async def test_the_completion_id_is_used_when_no_request_id_header_arrived() -> None:
    client = openai_client(
        create_result=openai_completion(request_id=None, completion_id="chatcmpl-fallback")
    )

    assert (await provider(client).generate(a_request())).request_id == "chatcmpl-fallback"


async def test_latency_is_measured_by_the_adapter() -> None:
    assert (await provider(openai_client()).generate(a_request())).latency_ms >= 0


async def test_null_content_becomes_an_empty_string() -> None:
    """A refusal or a tool-only turn leaves content unset; that is a real
    outcome, not a crash."""
    client = openai_client(create_result=openai_completion(text=None))

    assert (await provider(client).generate(a_request())).content == ""


async def test_a_completion_with_no_choices_is_handled() -> None:
    client = openai_client(create_result=openai_completion(choices=[]))

    assert (await provider(client).generate(a_request())).content == ""


async def test_no_sdk_object_reaches_the_caller() -> None:
    response = await provider(openai_client()).generate(a_request())

    assert type(response).__module__.startswith("app.ai")
    assert type(response.usage).__module__.startswith("app.ai")


# -- Structured output --------------------------------------------------------


async def test_structured_output_returns_a_validated_model() -> None:
    parsed = RefundSummary(pending=4, currency="GBP")
    client = openai_client(parse_result=openai_completion(parsed=parsed))

    structured = await provider(client).generate_structured(a_request(), RefundSummary)

    assert structured.data == parsed
    assert structured.response.provider == "openai"
    assert structured.response.usage.total_tokens == 154


async def test_structured_output_uses_the_native_mechanism() -> None:
    client = openai_client(
        parse_result=openai_completion(parsed=RefundSummary(pending=1, currency="GBP"))
    )

    await provider(client).generate_structured(a_request(), RefundSummary)

    assert client.chat.completions.parse.await_args.kwargs["response_format"] is RefundSummary
    client.chat.completions.create.assert_not_awaited()


async def test_missing_structured_output_is_an_invalid_response() -> None:
    client = openai_client(parse_result=openai_completion(parsed=None))

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_structured_output_of_the_wrong_type_is_rejected() -> None:
    class SomethingElse(BaseModel):
        other: str

    client = openai_client(parse_result=openai_completion(parsed=SomethingElse(other="x")))

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_a_completion_without_choices_has_no_structured_output() -> None:
    client = openai_client(parse_result=openai_completion(choices=[]))

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_a_schema_validation_failure_is_an_invalid_response() -> None:
    from pydantic import ValidationError

    try:
        RefundSummary.model_validate({"pending": "not a number"})
    except ValidationError as exc:
        failure = exc

    client = openai_client(parse_error=failure)

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_a_truncated_completion_is_an_invalid_response() -> None:
    """parse() raises when the output was cut short: the call succeeded but the
    structured result is unusable."""
    import openai

    # The SDK reads completion.usage while building its message.
    failure = openai.LengthFinishReasonError(
        completion=SimpleNamespace(  # type: ignore[arg-type]
            choices=[SimpleNamespace(finish_reason="length")],
            usage=None,
        )
    )
    client = openai_client(parse_error=failure)

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


# -- Error translation --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("authentication", LLMAuthenticationError),
        ("permission", LLMAuthenticationError),
        ("rate_limit", LLMRateLimitError),
        ("timeout", LLMTimeoutError),
        ("connection", LLMProviderError),
        ("not_found", LLMConfigurationError),
        ("server", LLMProviderError),
        ("bad_request", LLMProviderError),
    ],
)
async def test_sdk_errors_are_translated(kind: str, expected: type[LLMError]) -> None:
    client = openai_client(create_error=openai_errors()[kind])

    with pytest.raises(expected) as raised:
        await provider(client).generate(a_request())

    assert raised.value.provider == "openai"


async def test_different_failures_are_not_collapsed_into_one_error() -> None:
    errors = openai_errors()
    seen = set()

    for kind in ("authentication", "rate_limit", "timeout", "not_found", "server"):
        client = openai_client(create_error=errors[kind])
        with pytest.raises(LLMError) as raised:
            await provider(client).generate(a_request())
        seen.add(type(raised.value))

    assert len(seen) == 5


async def test_the_retry_hint_is_taken_from_the_provider() -> None:
    client = openai_client(create_error=openai_errors()["rate_limit"])

    with pytest.raises(LLMRateLimitError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.retry_after_seconds == 12.0


async def test_a_missing_retry_header_is_not_invented() -> None:
    client = openai_client(create_error=openai_errors()["rate_limit_no_header"])

    with pytest.raises(LLMRateLimitError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.retry_after_seconds is None


async def test_an_unexpected_failure_is_still_wrapped() -> None:
    client = openai_client(create_error=RuntimeError("something unforeseen"))

    with pytest.raises(LLMProviderError):
        await provider(client).generate(a_request())


async def test_the_original_cause_is_chained_for_the_log() -> None:
    original = openai_errors()["server"]
    client = openai_client(create_error=original)

    with pytest.raises(LLMError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.__cause__ is original


async def test_the_provider_message_is_not_passed_through_to_the_caller() -> None:
    client = openai_client(create_error=openai_errors()["server"])

    with pytest.raises(LLMError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.message == LLMProviderError.message


async def test_structured_errors_are_translated_too() -> None:
    client = openai_client(parse_error=openai_errors()["rate_limit"])

    with pytest.raises(LLMRateLimitError):
        await provider(client).generate_structured(a_request(), RefundSummary)


# -- Both adapters agree on the contract --------------------------------------


async def test_both_adapters_return_the_same_shape() -> None:
    """The point of the abstraction: a caller cannot tell them apart."""
    from app.ai.providers.anthropic import AnthropicProvider
    from tests.unit.ai_doubles import ANTHROPIC_KEY, anthropic_client

    anthropic_response = await AnthropicProvider(
        ANTHROPIC_KEY, timeout_seconds=30.0, client=anthropic_client()
    ).generate(a_request(model="claude-opus-5"))
    openai_response = await provider(openai_client()).generate(a_request())

    assert type(anthropic_response) is type(openai_response)
    assert set(anthropic_response.model_dump()) == set(openai_response.model_dump())
    assert anthropic_response.provider != openai_response.provider
