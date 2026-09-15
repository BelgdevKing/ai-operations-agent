"""Provider-independent request and response types.

These are the only shapes the rest of the application sees. Nothing here refers
to Anthropic, OpenAI or any SDK: a provider adapter translates between its own
wire format and these types, so swapping providers is a change in one package
rather than everywhere a model is called.

Two consequences of that rule are worth stating, because they look like
omissions otherwise:

* ``LLMRequest.model`` has no default. A default would have to name a real
  model, which would make this module provider-dependent and quietly tie the
  platform to one vendor. The model is chosen from configuration by the layer
  that builds the request.
* The system prompt is a message with ``role="system"``, not a separate field.
  Providers disagree about this - some take a top-level parameter, others an
  ordinary message - so the shared shape carries it the way that loses no
  information, and each adapter rearranges it.
"""

from __future__ import annotations

import enum
import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# Widest range any supported provider accepts. Individual providers accept
# narrower ranges; the adapter rejects a value its own API cannot take, rather
# than silently clamping it into something the caller did not ask for.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024


class LLMRole(enum.StrEnum):
    """Who authored a message.

    Distinct from ``app.models.enums.MessageRole``, which is the persisted
    conversation role: this one describes a request being built, and coupling
    the two would mean a schema change every time a provider adds a role.

    ``TOOL`` carries what a tool did, so an agent can hand a result back to the
    model and ask what to do next. It is provider-independent on purpose - see
    :class:`LLMToolResult`.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class LLMToolResult(BaseModel):
    """What a tool did, in terms no provider is involved in.

    Deliberately not the tool framework's own ``ToolResult``: importing that
    here would point the AI layer at the tool layer, which already points back
    at the agent layer, and the agent layer points here. The agent runtime
    translates between the two, which is the only place that knows both.

    Carries what a model needs to reason about the step - which tool ran, which
    execution it was, whether it worked, and the structured answer or the
    reason it did not.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(
        min_length=1,
        max_length=64,
        description="Identifies this execution, so the model can tell two calls "
        "of the same tool apart.",
    )
    succeeded: bool
    outcome: str = Field(
        min_length=1,
        max_length=64,
        description="The framework's own verdict, e.g. succeeded, not_found, "
        "approval_required. Lets the model distinguish 'no such shipment' from "
        "'that failed'.",
    )
    data: dict[str, Any] | None = Field(
        default=None, description="The tool's validated output, when it succeeded."
    )
    error: str | None = Field(
        default=None,
        max_length=2_000,
        description="Why it did not succeed. Already sanitised by the tool "
        "framework - never a provider's or a driver's own text.",
    )

    def render(self) -> str:
        """The text form a provider actually receives.

        Delimited and labelled so the boundary is legible in the transcript:
        everything between the markers is *data that a tool returned*, not an
        instruction from anybody. A model cannot be made to honour that by
        formatting alone - the real protection is that nothing in here can
        reach the tool framework - but an unmarked blob of business text would
        not even make the distinction visible.
        """
        verdict = "succeeded" if self.succeeded else f"failed ({self.outcome})"

        body = json.dumps(self.data, ensure_ascii=False, sort_keys=True, default=str)
        if not self.succeeded:
            body = self.error or "No further detail."

        header = f"[tool result: {self.tool_name} #{self.execution_id} {verdict}]"
        return "\n".join([header, body, "[end tool result]"])


class LLMMessage(BaseModel):
    """One message in a conversation sent to a model."""

    model_config = ConfigDict(frozen=True)

    role: LLMRole
    content: str = Field(
        min_length=1,
        description="Message text. Empty content is rejected: every provider "
        "treats it as an error, and failing here gives a better message than "
        "the provider's.",
    )
    tool: LLMToolResult | None = Field(
        default=None,
        description="Present on, and only on, a tool message.",
    )

    @model_validator(mode="after")
    def _tool_payload_matches_the_role(self) -> Self:
        """A tool message carries a result; nothing else does.

        Checked rather than trusted, because an adapter reading ``tool`` on a
        message that is not a tool turn - or finding nothing on one that is -
        would have no sensible behaviour to fall back on.
        """
        if self.role is LLMRole.TOOL and self.tool is None:
            raise ValueError("A tool message must carry a tool result.")
        if self.role is not LLMRole.TOOL and self.tool is not None:
            raise ValueError("Only a tool message may carry a tool result.")
        return self

    @property
    def transport_role(self) -> LLMRole:
        """The role a provider is actually sent.

        A tool turn goes as a user turn. Neither provider will accept a native
        tool message without a tool-call block it issued itself, and this
        architecture deliberately does not use provider tool calling - the
        model decides through structured output instead. Mapping it here rather
        than in each adapter keeps the two from drifting.
        """
        return LLMRole.USER if self.role is LLMRole.TOOL else self.role

    @property
    def transport_content(self) -> str:
        """The text a provider is actually sent."""
        return self.tool.render() if self.tool is not None else self.content

    @classmethod
    def system(cls, content: str) -> LLMMessage:
        """Convenience constructor for a system prompt."""
        return cls(role=LLMRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> LLMMessage:
        """Convenience constructor for a user turn."""
        return cls(role=LLMRole.USER, content=content)

    @classmethod
    def assistant(cls, content: str) -> LLMMessage:
        """Convenience constructor for an assistant turn."""
        return cls(role=LLMRole.ASSISTANT, content=content)

    @classmethod
    def tool_result(cls, result: LLMToolResult) -> LLMMessage:
        """A turn carrying what a tool did.

        ``content`` mirrors the rendered form so that anything reading the
        message as plain text - a log, a test, a future transcript - sees the
        same thing the provider will.
        """
        return cls(role=LLMRole.TOOL, content=result.render(), tool=result)


class LLMRequest(BaseModel):
    """What the application asks a model to do."""

    model_config = ConfigDict(frozen=True)

    messages: list[LLMMessage] = Field(
        min_length=1,
        description="Conversation so far, oldest first.",
    )
    model: str = Field(
        min_length=1,
        description="Provider-specific model identifier, supplied by the caller "
        "from configuration. Deliberately has no default - see the module "
        "docstring.",
    )
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        ge=MIN_TEMPERATURE,
        le=MAX_TEMPERATURE,
        description="Sampling temperature. Providers accept different ranges; "
        "an adapter rejects a value its API does not support.",
    )
    max_output_tokens: int = Field(
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        ge=1,
        description="Upper bound on tokens generated. Required by some "
        "providers, so the abstraction always sends one.",
    )

    @property
    def system_messages(self) -> list[LLMMessage]:
        """System messages, for adapters whose API takes them separately."""
        return [message for message in self.messages if message.role is LLMRole.SYSTEM]

    @property
    def conversation_messages(self) -> list[LLMMessage]:
        """Everything that is not a system message, in order."""
        return [message for message in self.messages if message.role is not LLMRole.SYSTEM]


class LLMUsage(BaseModel):
    """Token accounting for one call.

    Counts default to zero rather than being optional: a provider that reports
    nothing yields a usable record, and callers summing usage across calls do
    not have to special-case ``None``. ``total_tokens`` is derived so it can
    never disagree with its parts.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMResponse(BaseModel):
    """What a model returned, in provider-independent form."""

    model_config = ConfigDict(frozen=True)

    content: str = Field(description="Generated text. May be empty if the model produced none.")
    provider: str = Field(min_length=1, description="Name of the provider that served the call.")
    model: str = Field(
        min_length=1,
        description="Model that actually served the call, which can differ from "
        "the one requested when a provider resolves an alias.",
    )
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: float = Field(
        ge=0,
        description="Wall-clock duration measured by the gateway, not reported "
        "by the provider - so it is always present.",
    )
    request_id: str | None = Field(
        default=None,
        description="Provider's identifier for the call, for support requests. "
        "Optional: not every provider returns one.",
    )


class LLMStructuredResponse[DataT: BaseModel](BaseModel):
    """A response whose content has been parsed into a caller-supplied model.

    Composed rather than a subclass of :class:`LLMResponse`: the parsed value
    is the point, but token usage and latency still matter, and duplicating
    those fields would let the two copies drift.
    """

    model_config = ConfigDict(frozen=True)

    data: DataT = Field(description="Provider output validated against the requested schema.")
    response: LLMResponse = Field(description="The underlying call, including usage and latency.")
