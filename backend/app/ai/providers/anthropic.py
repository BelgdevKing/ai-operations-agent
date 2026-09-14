"""Anthropic adapter.

One of only two modules permitted to import a provider SDK. Everything it
returns is provider-independent, and every SDK exception it sees is translated
into ``app.ai.exceptions`` before leaving.

Two Anthropic-specific details are handled here rather than pushed onto
callers:

* **The system prompt is a top-level parameter**, not a message. Messages with
  ``role="system"`` are lifted out and joined into ``system``.
* **Current models reject sampling parameters.** ``temperature`` was removed
  from Claude Opus 5, Sonnet 5, Opus 4.8/4.7 and the Fable/Mythos family -
  sending it returns a 400. Since the default model is one of those, sending it
  unconditionally would fail every call, so it is omitted for those models.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Final

import anthropic
from pydantic import BaseModel, ValidationError

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.models import LLMRequest, LLMResponse, LLMStructuredResponse, LLMUsage
from app.ai.providers._support import Stopwatch, request_id_of, retry_after_seconds
from app.ai.providers.base import LLMProvider

logger = logging.getLogger(__name__)

PROVIDER_NAME: Final = "anthropic"

# Models that removed temperature/top_p/top_k and return 400 if they are sent.
# Matched as prefixes so dated snapshots of the same model are covered.
MODELS_WITHOUT_SAMPLING_CONTROL: Final[tuple[str, ...]] = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
)


def supports_sampling_parameters(model: str) -> bool:
    """Whether a model still accepts ``temperature``."""
    return not model.startswith(MODELS_WITHOUT_SAMPLING_CONTROL)


class AnthropicProvider(LLMProvider):
    """Serves completions from the Anthropic Messages API."""

    name: ClassVar[str] = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        """Build an adapter.

        Args:
            api_key: Anthropic credential. Held only by the SDK client; this
                object never stores or logs it.
            timeout_seconds: Ceiling on a single call, so a model request
                cannot hang a caller indefinitely.
            client: Pre-built client, for tests. Production passes nothing.
        """
        if not api_key:
            raise LLMConfigurationError("No Anthropic API key is configured.", provider=self.name)

        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_seconds,
            # Retries belong to the gateway, which is the only layer that can
            # see the whole operation. Leaving the SDK's own retries on would
            # multiply attempts (3 x 3 = 9 calls) and make the gateway's bound
            # meaningless.
            max_retries=0,
        )

    def __repr__(self) -> str:
        # Explicit, so a default repr can never grow a field that exposes the
        # client and through it the credential.
        return f"<{type(self).__name__} name={self.name!r}>"

    # -- Public interface -----------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        stopwatch = Stopwatch()
        try:
            message = await self._client.messages.create(**self._build_arguments(request))
        except Exception as exc:
            raise self._translate(exc) from exc

        return self._to_response(message, stopwatch.elapsed_ms)

    async def generate_structured[DataT: BaseModel](
        self,
        request: LLMRequest,
        schema: type[DataT],
    ) -> LLMStructuredResponse[DataT]:
        """Use the Messages API's native structured output.

        ``messages.parse`` constrains generation to the schema and validates
        the result, so this does not prompt for JSON and hope.
        """
        stopwatch = Stopwatch()
        try:
            message = await self._client.messages.parse(
                output_format=schema,
                **self._build_arguments(request),
            )
        except ValidationError as exc:
            # Output that does not satisfy the schema: a prompt or schema
            # problem, not an unwell provider.
            raise self._invalid_response(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc

        parsed = getattr(message, "parsed_output", None)
        if parsed is None:
            raise LLMInvalidResponseError(
                "The model returned no structured output.", provider=self.name
            )
        if not isinstance(parsed, schema):
            # Defensive: the SDK validates, but a provider-independent layer
            # must not hand back something of the wrong type.
            raise LLMInvalidResponseError(
                "The model's structured output did not match the requested schema.",
                provider=self.name,
            )

        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=parsed,
            response=self._to_response(message, stopwatch.elapsed_ms),
        )

    # -- Translation into the Anthropic format --------------------------------

    def _build_arguments(self, request: LLMRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.conversation_messages
            ],
        }

        system = "\n\n".join(message.content for message in request.system_messages)
        if system:
            arguments["system"] = system

        if supports_sampling_parameters(request.model):
            arguments["temperature"] = request.temperature
        elif "temperature" in request.model_fields_set:
            # Only worth saying when the caller actually chose a value; the
            # default being dropped is not news.
            logger.warning(
                "Dropping temperature: model does not accept sampling parameters",
                extra={"context": {"provider": self.name, "model": request.model}},
            )

        return arguments

    # -- Translation back into provider-independent types ---------------------

    def _to_response(self, message: object, latency_ms: float) -> LLMResponse:
        blocks = getattr(message, "content", None) or []
        text = "".join(
            block.text
            for block in blocks
            if getattr(block, "type", None) == "text"
            and isinstance(getattr(block, "text", None), str)
        )
        usage = getattr(message, "usage", None)

        return LLMResponse(
            content=text,
            provider=self.name,
            # The model the provider actually served, which can differ from the
            # one requested when an alias is resolved.
            model=getattr(message, "model", None) or "unknown",
            usage=LLMUsage(
                input_tokens=max(int(getattr(usage, "input_tokens", 0) or 0), 0),
                output_tokens=max(int(getattr(usage, "output_tokens", 0) or 0), 0),
            ),
            latency_ms=latency_ms,
            request_id=request_id_of(message),
        )

    # -- Error translation ----------------------------------------------------

    def _invalid_response(self, exc: Exception) -> LLMInvalidResponseError:
        self._log(exc)
        return LLMInvalidResponseError(provider=self.name)

    def _translate(self, exc: Exception) -> Exception:
        """Turn an SDK exception into one of ours.

        Distinct branches on purpose: collapsing everything into one error
        would hide the difference between "retry shortly", "our key is wrong"
        and "the request was malformed". The provider's own message is logged
        but never returned - it can name models, quotas and endpoints.
        """
        self._log(exc)

        match exc:
            case anthropic.AuthenticationError() | anthropic.PermissionDeniedError():
                return LLMAuthenticationError(provider=self.name)
            case anthropic.RateLimitError():
                return LLMRateLimitError(
                    provider=self.name,
                    retry_after_seconds=retry_after_seconds(exc),
                )
            case anthropic.APITimeoutError():
                return LLMTimeoutError(provider=self.name)
            case anthropic.NotFoundError():
                # An unknown or unavailable model: a configuration problem, and
                # no amount of retrying fixes it.
                return LLMConfigurationError(
                    "The configured Anthropic model is not available.", provider=self.name
                )
            case anthropic.APIConnectionError():
                # Covers timeouts' sibling cases: DNS, TLS, refused connections.
                return LLMProviderError(
                    "Could not reach the language model provider.", provider=self.name
                )
            case anthropic.APIStatusError():
                return LLMProviderError(provider=self.name)
            case _:
                # Anything unrecognised, including a non-SDK failure. Still
                # wrapped, so no foreign exception type escapes this package.
                return LLMProviderError(provider=self.name)

    def _log(self, exc: Exception) -> None:
        """Record the real cause server-side.

        The exception type and HTTP status are enough to diagnose without
        copying provider response bodies into the log; the chained ``__cause__``
        carries the rest into any traceback.
        """
        logger.warning(
            "Anthropic call failed",
            extra={
                "context": {
                    "provider": self.name,
                    "error": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                }
            },
        )
