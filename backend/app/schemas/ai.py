"""Request and response schemas for the AI endpoint.

Provider-independent, and deliberately narrower than the internal
:mod:`app.ai.models` types: this is the public contract, so every limit is
explicit and appears in the OpenAPI schema.

Two things the request cannot do, both enforced rather than documented:

* **Choose a provider.** ``extra="forbid"`` means a body carrying
  ``"provider": "openai"`` is rejected with a 422 rather than silently ignored,
  so an attempt to route around the server's configuration is visible instead
  of quiet.
* **Choose an arbitrary model.** ``model`` is optional; the server's configured
  model is used unless the request names one the deployment has allowed.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.models import (
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    LLMMessage,
    LLMRole,
)

# Conservative ceilings. They exist to stop a single request consuming an
# unreasonable amount of provider budget or memory, not to be a quota - there
# is no billing or rate limiting yet, so these are the only bound on one call.
MAX_MESSAGES = 50
MAX_MESSAGE_CHARACTERS = 10_000
MAX_TOTAL_CHARACTERS = 50_000

# Below any provider's real ceiling, so a request that passes here fails for
# reasons a caller can act on rather than a provider-specific limit.
MAX_OUTPUT_TOKENS_LIMIT = 4_096


class AIMessage(BaseModel):
    """One message in the conversation sent to the model."""

    model_config = ConfigDict(extra="forbid")

    role: LLMRole = Field(description="system, user or assistant.")
    content: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_CHARACTERS,
        description=f"Message text, at most {MAX_MESSAGE_CHARACTERS:,} characters.",
    )

    def to_llm_message(self) -> LLMMessage:
        """Convert to the internal, provider-independent message."""
        return LLMMessage(role=self.role, content=self.content)


class GenerateRequest(BaseModel):
    """Ask the configured model for a completion."""

    model_config = ConfigDict(extra="forbid")

    messages: list[AIMessage] = Field(
        min_length=1,
        max_length=MAX_MESSAGES,
        description=f"Conversation so far, oldest first. At most {MAX_MESSAGES} messages.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional. Omit to use the model this deployment is configured with. "
            "A named model must be one the deployment allows; the provider is "
            "never selectable from a request."
        ),
    )
    temperature: Annotated[float, Field(ge=MIN_TEMPERATURE, le=MAX_TEMPERATURE)] | None = Field(
        default=None,
        description="Optional sampling temperature. Some models do not accept one.",
    )
    max_output_tokens: Annotated[int, Field(ge=1, le=MAX_OUTPUT_TOKENS_LIMIT)] | None = Field(
        default=None,
        description=f"Optional cap on generated tokens, at most {MAX_OUTPUT_TOKENS_LIMIT:,}.",
    )

    @model_validator(mode="after")
    def _limit_total_size(self) -> Self:
        """Bound the whole conversation, not just each message.

        Fifty messages of ten thousand characters would otherwise be a valid
        half-megabyte prompt.
        """
        total = sum(len(message.content) for message in self.messages)
        if total > MAX_TOTAL_CHARACTERS:
            raise ValueError(
                f"The conversation is {total:,} characters; the limit is {MAX_TOTAL_CHARACTERS:,}."
            )
        return self


class AIUsage(BaseModel):
    """Tokens consumed by the call."""

    model_config = ConfigDict(from_attributes=True)

    input_tokens: int
    output_tokens: int
    total_tokens: int


class GenerateResponse(BaseModel):
    """The model's answer.

    Built from explicit fields, so nothing from a provider SDK can reach a
    client through it.

    Two deliberate omissions:

    * **No provider name.** Which vendor served the call is internal routing,
      not part of the contract. The model identifier does imply it - callers
      need to know what produced the text - but naming the provider outright
      would make a deployment detail something clients could depend on.
    * **No provider request id.** The ``X-Request-ID`` response header already
      gives a caller something to quote in a support request, and it correlates
      with this application's logs rather than a vendor's.
    """

    content: str = Field(description="Generated text.")
    model: str = Field(description="Model that served the call.")
    usage: AIUsage
    latency_ms: float = Field(description="Duration of the provider call, in milliseconds.")
