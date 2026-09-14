"""OpenAI adapter.

The second of the two modules permitted to import a provider SDK. Same
contract as the Anthropic adapter: provider-independent in, provider-independent
out, and no SDK exception type escapes.

Where it differs from the Anthropic adapter, and why:

* **The system prompt is an ordinary message.** OpenAI takes it in the message
  list, so system messages are kept in place rather than lifted out.
* **``temperature`` is sent only when the caller explicitly set one.** OpenAI's
  reasoning models reject a non-default temperature, and there is no way to
  enumerate which from the SDK. Omitting it unless asked means the provider's
  own default applies and the common path always works; a caller who really
  wants a temperature still gets a clear error if the model refuses.
* **``max_completion_tokens``**, not ``max_tokens`` - the latter is rejected by
  current models.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Final

import openai
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

PROVIDER_NAME: Final = "openai"


class OpenAIProvider(LLMProvider):
    """Serves completions from the OpenAI Chat Completions API."""

    name: ClassVar[str] = PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        """Build an adapter.

        Args:
            api_key: OpenAI credential. Held only by the SDK client; this
                object never stores or logs it.
            timeout_seconds: Ceiling on a single call.
            client: Pre-built client, for tests. Production passes nothing.
        """
        if not api_key:
            raise LLMConfigurationError("No OpenAI API key is configured.", provider=self.name)

        self._client = client or openai.AsyncOpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            # Retries belong to the gateway, which is the only layer that can
            # see the whole operation. Leaving the SDK's own retries on would
            # multiply attempts (3 x 3 = 9 calls) and make the gateway's bound
            # meaningless.
            max_retries=0,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"

    # -- Public interface -----------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        stopwatch = Stopwatch()
        try:
            completion = await self._client.chat.completions.create(
                **self._build_arguments(request)
            )
        except Exception as exc:
            raise self._translate(exc) from exc

        return self._to_response(completion, stopwatch.elapsed_ms)

    async def generate_structured[DataT: BaseModel](
        self,
        request: LLMRequest,
        schema: type[DataT],
    ) -> LLMStructuredResponse[DataT]:
        """Use OpenAI's native structured output.

        ``chat.completions.parse`` constrains generation to the schema and
        validates the result, so this does not prompt for JSON and hope.
        """
        stopwatch = Stopwatch()
        try:
            completion = await self._client.chat.completions.parse(
                response_format=schema,
                **self._build_arguments(request),
            )
        except ValidationError as exc:
            raise self._invalid_response(exc) from exc
        except Exception as exc:
            raise self._translate(exc) from exc

        parsed = getattr(self._first_message(completion), "parsed", None)
        if parsed is None:
            raise LLMInvalidResponseError(
                "The model returned no structured output.", provider=self.name
            )
        if not isinstance(parsed, schema):
            raise LLMInvalidResponseError(
                "The model's structured output did not match the requested schema.",
                provider=self.name,
            )

        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=parsed,
            response=self._to_response(completion, stopwatch.elapsed_ms),
        )

    # -- Translation into the OpenAI format -----------------------------------

    def _build_arguments(self, request: LLMRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": request.model,
            # System messages stay in place: OpenAI takes them as messages.
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "max_completion_tokens": request.max_output_tokens,
        }

        if "temperature" in request.model_fields_set:
            arguments["temperature"] = request.temperature

        return arguments

    # -- Translation back into provider-independent types ---------------------

    @staticmethod
    def _first_message(completion: object) -> object:
        choices = getattr(completion, "choices", None) or []
        return getattr(choices[0], "message", None) if choices else None

    def _to_response(self, completion: object, latency_ms: float) -> LLMResponse:
        message = self._first_message(completion)
        content = getattr(message, "content", None) if message is not None else None
        usage = getattr(completion, "usage", None)

        return LLMResponse(
            # A refusal or a tool-only turn can leave content unset; an empty
            # string is the honest representation, not a crash.
            content=content if isinstance(content, str) else "",
            provider=self.name,
            model=getattr(completion, "model", None) or "unknown",
            usage=LLMUsage(
                input_tokens=max(int(getattr(usage, "prompt_tokens", 0) or 0), 0),
                output_tokens=max(int(getattr(usage, "completion_tokens", 0) or 0), 0),
            ),
            latency_ms=latency_ms,
            request_id=request_id_of(completion),
        )

    # -- Error translation ----------------------------------------------------

    def _invalid_response(self, exc: Exception) -> LLMInvalidResponseError:
        self._log(exc)
        return LLMInvalidResponseError(provider=self.name)

    def _translate(self, exc: Exception) -> Exception:
        """Turn an SDK exception into one of ours.

        Distinct branches on purpose, and the provider's own message is logged
        rather than returned.
        """
        self._log(exc)

        match exc:
            case openai.AuthenticationError() | openai.PermissionDeniedError():
                return LLMAuthenticationError(provider=self.name)
            case openai.RateLimitError():
                return LLMRateLimitError(
                    provider=self.name,
                    retry_after_seconds=retry_after_seconds(exc),
                )
            case openai.APITimeoutError():
                return LLMTimeoutError(provider=self.name)
            case openai.NotFoundError():
                return LLMConfigurationError(
                    "The configured OpenAI model is not available.", provider=self.name
                )
            case openai.LengthFinishReasonError() | openai.ContentFilterFinishReasonError():
                # Raised by parse() when the completion was cut short or
                # filtered: the call succeeded but the output is unusable.
                return LLMInvalidResponseError(provider=self.name)
            case openai.APIConnectionError():
                return LLMProviderError(
                    "Could not reach the language model provider.", provider=self.name
                )
            case openai.APIStatusError():
                return LLMProviderError(provider=self.name)
            case _:
                return LLMProviderError(provider=self.name)

    def _log(self, exc: Exception) -> None:
        logger.warning(
            "OpenAI call failed",
            extra={
                "context": {
                    "provider": self.name,
                    "error": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                }
            },
        )
