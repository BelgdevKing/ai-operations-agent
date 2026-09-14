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

from pydantic import BaseModel, ConfigDict, Field, computed_field

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
    conversation role and also carries ``tool``. Tool results are not part of
    this abstraction yet, and coupling the request format to a database enum
    would mean a schema change every time a provider adds a role.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


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
