"""The Anthropic adapter. Offline: the SDK client is injected."""

from __future__ import annotations

import logging
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
from app.ai.providers.anthropic import AnthropicProvider, supports_sampling_parameters
from tests.unit.ai_doubles import (
    ANTHROPIC_KEY,
    anthropic_client,
    anthropic_errors,
    anthropic_message,
)

MODEL = "claude-opus-5"
SAMPLING_MODEL = "claude-haiku-4-5"


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


def provider(client: Any) -> AnthropicProvider:
    return AnthropicProvider(ANTHROPIC_KEY, timeout_seconds=30.0, client=client)


def sent(client: Any) -> dict[str, Any]:
    """The keyword arguments the adapter passed to the SDK."""
    return dict(client.messages.create.await_args.kwargs)


# -- Construction -------------------------------------------------------------


def test_an_empty_key_is_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError):
        AnthropicProvider("", timeout_seconds=30.0)


def test_the_adapter_names_itself() -> None:
    assert AnthropicProvider.name == "anthropic"


# -- Request translation ------------------------------------------------------


async def test_the_request_is_translated_into_the_anthropic_format() -> None:
    client = anthropic_client()

    await provider(client).generate(a_request(max_output_tokens=512))

    arguments = sent(client)
    assert arguments["model"] == MODEL
    assert arguments["max_tokens"] == 512
    assert arguments["messages"] == [{"role": "user", "content": "How many refunds are pending?"}]


async def test_system_messages_become_the_top_level_system_parameter() -> None:
    """Anthropic takes the system prompt as a parameter, not a message."""
    client = anthropic_client()
    request = a_request(
        messages=[
            LLMMessage.system("Be concise."),
            LLMMessage.user("Question?"),
            LLMMessage.assistant("Answer."),
        ]
    )

    await provider(client).generate(request)

    arguments = sent(client)
    assert arguments["system"] == "Be concise."
    assert [m["role"] for m in arguments["messages"]] == ["user", "assistant"]
    assert all(m["role"] != "system" for m in arguments["messages"])


async def test_several_system_messages_are_joined() -> None:
    client = anthropic_client()
    request = a_request(
        messages=[
            LLMMessage.system("Be concise."),
            LLMMessage.user("Question?"),
            LLMMessage.system("Be polite."),
        ]
    )

    await provider(client).generate(request)

    assert sent(client)["system"] == "Be concise.\n\nBe polite."


async def test_no_system_parameter_is_sent_when_there_is_none() -> None:
    client = anthropic_client()

    await provider(client).generate(a_request())

    assert "system" not in sent(client)


# -- Sampling parameters ------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5"],
)
async def test_temperature_is_not_sent_to_models_that_reject_it(model: str) -> None:
    """Current models removed sampling parameters and return 400 if sent.

    The default model is one of them, so sending temperature unconditionally
    would fail every call.
    """
    client = anthropic_client()

    await provider(client).generate(a_request(model=model))

    assert "temperature" not in sent(client)
    assert supports_sampling_parameters(model) is False


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-4-6", "claude-sonnet-4-6"])
async def test_temperature_is_sent_to_models_that_accept_it(model: str) -> None:
    client = anthropic_client()

    await provider(client).generate(a_request(model=model, temperature=0.2))

    assert sent(client)["temperature"] == 0.2
    assert supports_sampling_parameters(model) is True


async def test_dropping_an_explicit_temperature_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silently discarding what a caller asked for would be worse than saying so."""
    client = anthropic_client()

    with caplog.at_level(logging.WARNING, logger="app.ai.providers.anthropic"):
        await provider(client).generate(a_request(model=MODEL, temperature=0.2))

    assert "Dropping temperature" in caplog.text


async def test_a_defaulted_temperature_is_dropped_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default being dropped is not news, and would be noise on every call."""
    client = anthropic_client()

    with caplog.at_level(logging.WARNING, logger="app.ai.providers.anthropic"):
        await provider(client).generate(a_request(model=MODEL))

    assert "Dropping temperature" not in caplog.text


# -- Response translation -----------------------------------------------------


async def test_the_response_is_translated_back() -> None:
    client = anthropic_client(create_result=anthropic_message(text="Four."))

    response = await provider(client).generate(a_request())

    assert isinstance(response, LLMResponse)
    assert response.content == "Four."
    assert response.provider == "anthropic"


async def test_usage_is_translated() -> None:
    client = anthropic_client(create_result=anthropic_message(input_tokens=1200, output_tokens=40))

    response = await provider(client).generate(a_request())

    assert response.usage.input_tokens == 1200
    assert response.usage.output_tokens == 40
    assert response.usage.total_tokens == 1240


async def test_the_model_reported_is_the_one_that_served_the_call() -> None:
    """Not the one requested: a provider may resolve an alias."""
    client = anthropic_client(create_result=anthropic_message(model="claude-opus-5-resolved"))

    response = await provider(client).generate(a_request(model="claude-opus-5"))

    assert response.model == "claude-opus-5-resolved"


async def test_the_request_id_is_captured() -> None:
    client = anthropic_client(create_result=anthropic_message(request_id="req_xyz"))

    assert (await provider(client).generate(a_request())).request_id == "req_xyz"


async def test_the_message_id_is_used_when_no_request_id_header_arrived() -> None:
    client = anthropic_client(
        create_result=anthropic_message(request_id=None, message_id="msg_fallback")
    )

    assert (await provider(client).generate(a_request())).request_id == "msg_fallback"


async def test_latency_is_measured_by_the_adapter() -> None:
    """Providers do not report it, and what matters is what the caller waited."""
    client = anthropic_client()

    assert (await provider(client).generate(a_request())).latency_ms >= 0


async def test_several_text_blocks_are_concatenated() -> None:
    from types import SimpleNamespace

    client = anthropic_client(
        create_result=anthropic_message(
            content=[
                SimpleNamespace(type="text", text="One. "),
                SimpleNamespace(type="thinking", thinking="ignored"),
                SimpleNamespace(type="text", text="Two."),
            ]
        )
    )

    assert (await provider(client).generate(a_request())).content == "One. Two."


async def test_a_response_with_no_text_yields_empty_content() -> None:
    client = anthropic_client(create_result=anthropic_message(content=[]))

    assert (await provider(client).generate(a_request())).content == ""


async def test_no_sdk_object_reaches_the_caller() -> None:
    response = await provider(anthropic_client()).generate(a_request())

    assert type(response).__module__.startswith("app.ai")
    assert type(response.usage).__module__.startswith("app.ai")


# -- Structured output --------------------------------------------------------


async def test_structured_output_returns_a_validated_model() -> None:
    parsed = RefundSummary(pending=4, currency="GBP")
    client = anthropic_client(parse_result=anthropic_message(parsed_output=parsed))

    structured = await provider(client).generate_structured(a_request(), RefundSummary)

    assert structured.data == parsed
    assert structured.response.provider == "anthropic"
    assert structured.response.usage.total_tokens == 154


async def test_structured_output_uses_the_native_mechanism() -> None:
    """The schema is sent to the provider rather than requested in a prompt."""
    client = anthropic_client(
        parse_result=anthropic_message(parsed_output=RefundSummary(pending=1, currency="GBP"))
    )

    await provider(client).generate_structured(a_request(), RefundSummary)

    assert client.messages.parse.await_args.kwargs["output_format"] is RefundSummary
    client.messages.create.assert_not_awaited()


async def test_missing_structured_output_is_an_invalid_response() -> None:
    client = anthropic_client(parse_result=anthropic_message(parsed_output=None))

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_structured_output_of_the_wrong_type_is_rejected() -> None:
    """A provider-independent layer must not hand back an unchecked object."""

    class SomethingElse(BaseModel):
        other: str

    client = anthropic_client(
        parse_result=anthropic_message(parsed_output=SomethingElse(other="x"))
    )

    with pytest.raises(LLMInvalidResponseError):
        await provider(client).generate_structured(a_request(), RefundSummary)


async def test_a_schema_validation_failure_is_an_invalid_response() -> None:
    from pydantic import ValidationError

    try:
        RefundSummary.model_validate({"pending": "not a number"})
    except ValidationError as exc:
        failure = exc

    client = anthropic_client(parse_error=failure)

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
    client = anthropic_client(create_error=anthropic_errors()[kind])

    with pytest.raises(expected) as raised:
        await provider(client).generate(a_request())

    assert raised.value.provider == "anthropic"


async def test_different_failures_are_not_collapsed_into_one_error() -> None:
    """The remedies differ: retry, fix the key, fix the model name."""
    errors = anthropic_errors()
    seen = set()

    for kind in ("authentication", "rate_limit", "timeout", "not_found", "server"):
        client = anthropic_client(create_error=errors[kind])
        with pytest.raises(LLMError) as raised:
            await provider(client).generate(a_request())
        seen.add(type(raised.value))

    assert len(seen) == 5


async def test_the_retry_hint_is_taken_from_the_provider() -> None:
    client = anthropic_client(create_error=anthropic_errors()["rate_limit"])

    with pytest.raises(LLMRateLimitError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.retry_after_seconds == 30.0


async def test_a_missing_retry_header_is_not_invented() -> None:
    client = anthropic_client(create_error=anthropic_errors()["rate_limit_no_header"])

    with pytest.raises(LLMRateLimitError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.retry_after_seconds is None


async def test_an_unexpected_failure_is_still_wrapped() -> None:
    """No foreign exception type may escape the abstraction."""
    client = anthropic_client(create_error=RuntimeError("something unforeseen"))

    with pytest.raises(LLMProviderError):
        await provider(client).generate(a_request())


async def test_the_original_cause_is_chained_for_the_log() -> None:
    original = anthropic_errors()["server"]
    client = anthropic_client(create_error=original)

    with pytest.raises(LLMError) as raised:
        await provider(client).generate(a_request())

    assert raised.value.__cause__ is original


async def test_the_provider_message_is_not_passed_through_to_the_caller() -> None:
    """Provider text can name models, quotas and endpoints."""
    client = anthropic_client(
        create_error=anthropic_errors()["server"],
    )

    with pytest.raises(LLMError) as raised:
        await provider(client).generate(a_request())

    assert "overloaded" not in raised.value.message
    assert raised.value.message == LLMProviderError.message


async def test_structured_errors_are_translated_too() -> None:
    client = anthropic_client(parse_error=anthropic_errors()["rate_limit"])

    with pytest.raises(LLMRateLimitError):
        await provider(client).generate_structured(a_request(), RefundSummary)
