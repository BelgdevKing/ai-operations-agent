"""How each adapter puts a conversation on the wire.

Serialization only: the SDK clients are never constructed and no network call is
made. What is under test is the mapping from the provider-independent message
model into each vendor's argument shape - the one place a tool turn becomes
something a provider will accept.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.ai.models import LLMMessage, LLMRequest, LLMRole, LLMToolResult
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.openai import OpenAIProvider

MODEL = "test-model"

SHIPMENT = {"shipment_reference": "ABC123", "status": "in_transit"}


def a_tool_result(**overrides: Any) -> LLMToolResult:
    defaults: dict[str, Any] = {
        "tool_name": "get_shipment",
        "execution_id": "exec-1",
        "succeeded": True,
        "outcome": "succeeded",
        "data": SHIPMENT,
    }
    return LLMToolResult(**{**defaults, **overrides})


def a_conversation() -> list[LLMMessage]:
    """The shape a real run produces: prompt, question, request, result."""
    return [
        LLMMessage.system("You are an operations assistant."),
        LLMMessage.user("Where is ABC123?"),
        LLMMessage.assistant("[tool request: get_shipment]"),
        LLMMessage.tool_result(a_tool_result()),
    ]


def anthropic_arguments(messages: list[LLMMessage]) -> dict[str, Any]:
    """Build Anthropic's arguments without constructing a client."""
    request = LLMRequest(messages=messages, model=MODEL)
    return AnthropicProvider._build_arguments(  # type: ignore[misc]
        AnthropicProvider.__new__(AnthropicProvider), request
    )


def openai_arguments(messages: list[LLMMessage]) -> dict[str, Any]:
    request = LLMRequest(messages=messages, model=MODEL)
    return OpenAIProvider._build_arguments(  # type: ignore[misc]
        OpenAIProvider.__new__(OpenAIProvider), request
    )


# -- Anthropic ----------------------------------------------------------------


def test_anthropic_keeps_ordinary_turns_unchanged() -> None:
    arguments = anthropic_arguments([LLMMessage.user("Hello"), LLMMessage.assistant("Hi")])

    assert arguments["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]


def test_anthropic_lifts_the_system_prompt_out_of_the_messages() -> None:
    arguments = anthropic_arguments(a_conversation())

    assert arguments["system"] == "You are an operations assistant."
    assert all(message["role"] != "system" for message in arguments["messages"])


def test_anthropic_sends_a_tool_turn_as_a_user_turn() -> None:
    """The Messages API refuses a tool_result block without a tool_use it
    issued, and this architecture does not use provider tool calling."""
    arguments = anthropic_arguments(a_conversation())

    assert arguments["messages"][-1]["role"] == "user"


def test_anthropic_preserves_message_order() -> None:
    arguments = anthropic_arguments(a_conversation())

    assert [message["role"] for message in arguments["messages"]] == [
        "user",
        "assistant",
        "user",
    ]


def test_anthropic_preserves_tool_identity_and_result() -> None:
    arguments = anthropic_arguments(a_conversation())
    rendered = arguments["messages"][-1]["content"]

    assert "get_shipment" in rendered
    assert "exec-1" in rendered
    assert "ABC123" in rendered
    assert "succeeded" in rendered


# -- OpenAI -------------------------------------------------------------------


def test_openai_keeps_ordinary_turns_unchanged() -> None:
    arguments = openai_arguments([LLMMessage.user("Hello"), LLMMessage.assistant("Hi")])

    assert arguments["messages"] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]


def test_openai_keeps_the_system_prompt_in_the_messages() -> None:
    arguments = openai_arguments(a_conversation())

    assert arguments["messages"][0] == {
        "role": "system",
        "content": "You are an operations assistant.",
    }


def test_openai_sends_a_tool_turn_as_a_user_turn() -> None:
    """The native tool role needs a tool_call_id from a tool_calls block."""
    arguments = openai_arguments(a_conversation())

    assert arguments["messages"][-1]["role"] == "user"
    assert all(message["role"] != "tool" for message in arguments["messages"])


def test_openai_preserves_message_order() -> None:
    arguments = openai_arguments(a_conversation())

    assert [message["role"] for message in arguments["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


def test_openai_preserves_tool_identity_and_result() -> None:
    arguments = openai_arguments(a_conversation())
    rendered = arguments["messages"][-1]["content"]

    assert "get_shipment" in rendered
    assert "exec-1" in rendered
    assert "ABC123" in rendered


# -- Both adapters agree ------------------------------------------------------


def test_both_providers_render_a_tool_turn_identically() -> None:
    """The rendering is the abstraction's, not each vendor's - so a tool result
    cannot come to mean different things on different providers."""
    conversation = a_conversation()

    anthropic = anthropic_arguments(conversation)["messages"][-1]["content"]
    openai = openai_arguments(conversation)["messages"][-1]["content"]

    assert anthropic == openai


def test_a_failed_tool_says_why_without_naming_anything_internal() -> None:
    failed = a_tool_result(
        succeeded=False,
        outcome="not_found",
        data=None,
        error="No shipment was found with that reference.",
    )
    arguments = openai_arguments([LLMMessage.user("Where is it?"), LLMMessage.tool_result(failed)])
    rendered = arguments["messages"][-1]["content"]

    assert "failed (not_found)" in rendered
    assert "No shipment was found" in rendered


def test_a_tool_turn_is_delimited_so_the_boundary_is_legible() -> None:
    """A model cannot be made to respect this by formatting alone, but an
    unmarked blob of business text would not even make the boundary visible."""
    rendered = openai_arguments(a_conversation())["messages"][-1]["content"]

    assert rendered.startswith("[tool result:")
    assert rendered.rstrip().endswith("[end tool result]")


# -- Malformed messages never reach a provider --------------------------------


def test_a_tool_message_without_a_result_is_refused_before_any_adapter() -> None:
    with pytest.raises(ValidationError):
        LLMMessage(role=LLMRole.TOOL, content="anything")


def test_a_tool_result_needs_a_name_and_an_execution_id() -> None:
    with pytest.raises(ValidationError):
        LLMToolResult(tool_name="", execution_id="e", succeeded=True, outcome="succeeded")
    with pytest.raises(ValidationError):
        LLMToolResult(tool_name="t", execution_id="", succeeded=True, outcome="succeeded")


def test_a_tool_result_needs_an_outcome() -> None:
    with pytest.raises(ValidationError):
        LLMToolResult(tool_name="t", execution_id="e", succeeded=False, outcome="")
