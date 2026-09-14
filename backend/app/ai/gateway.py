"""The gateway: the application's single entry point for model calls.

Services depend on this, never on a provider adapter. It owns everything that
is the same whoever serves the call - choosing the provider, deciding what is
worth retrying, backing off, correlating, and recording what happened - and
delegates everything provider-shaped to the adapter underneath.

    application  ->  LLMGateway  ->  LLMProvider  ->  Anthropic / OpenAI

**Latency.** ``LLMResponse.latency_ms`` is the provider call itself, set by the
adapter, and the gateway does not touch it: it is what cost and provider
performance should be judged on. The gateway's own duration - the whole
operation, including failed attempts and the waits between them - is recorded
in the log as ``gateway_latency_ms``. A response therefore comes back from
:meth:`LLMGateway.generate` exactly as the adapter produced it.

**Attempt bound.** At most ``max_retries + 1`` calls reach a provider, three by
default. The adapters construct their SDK clients with ``max_retries=0`` so
this is the only retry layer; worst-case wall time is roughly that many
provider timeouts plus the backoff waits, each capped at
``llm_retry_max_delay_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel

from app.ai.exceptions import (
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.models import LLMRequest, LLMResponse, LLMStructuredResponse
from app.ai.providers.base import LLMProvider

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES: Final = 2
DEFAULT_BASE_DELAY_SECONDS: Final = 0.5
DEFAULT_MAX_DELAY_SECONDS: Final = 8.0

# Retryable 4xx. Everything else in that range says the request itself is
# wrong, and sending it again produces the same answer more slowly.
_RETRYABLE_CLIENT_STATUSES: Final = frozenset({408, 429})

Sleeper = Callable[[float], Awaitable[None]]
"""Injectable delay, so tests never actually wait."""

Jitter = Callable[[float, float], float]
"""Injectable randomness, so a test can make backoff deterministic."""


def is_retryable(error: LLMError) -> bool:
    """Whether trying the same request again could plausibly succeed.

    Rate limits and timeouts are transient by definition. Authentication,
    configuration and invalid-response failures are not: the key is wrong, the
    model name is wrong, or the output did not fit the schema, and none of
    those change on a second attempt.

    ``LLMProviderError`` covers both transient faults (connection dropped, 502,
    overloaded) and permanent ones (a malformed request). They are separated by
    looking at the status code on the chained cause when there is one - no SDK
    import, just an attribute every HTTP client exposes. Without a status, the
    failure was almost certainly a connection problem, which is worth retrying.
    """
    if isinstance(error, LLMRateLimitError | LLMTimeoutError):
        return True

    if isinstance(error, LLMProviderError):
        status = getattr(error.__cause__, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return status in _RETRYABLE_CLIENT_STATUSES
        return True

    return False


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try again, and how long to wait between attempts."""

    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS

    @classmethod
    def from_settings(cls, settings: Settings) -> RetryPolicy:
        return cls(
            max_retries=settings.llm_max_retries,
            max_delay_seconds=settings.llm_retry_max_delay_seconds,
        )

    @property
    def max_attempts(self) -> int:
        """Total calls that may reach a provider, including the first."""
        return self.max_retries + 1

    def backoff_ceiling(self, attempt: int) -> float:
        """Longest the wait after *attempt* may be.

        Doubles per attempt and is capped, so a long outage does not turn into
        a long wait.
        """
        return min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)


class LLMGateway:
    """Provider-independent access to a language model.

    Construct it from settings in the application, or with an explicit provider
    in tests:

        gateway = LLMGateway.from_settings(settings)
        gateway = LLMGateway(provider=FakeProvider(), sleep=no_sleep)
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        retry: RetryPolicy | None = None,
        sleep: Sleeper | None = None,
        jitter: Jitter | None = None,
    ) -> None:
        """Build a gateway over a provider.

        Args:
            provider: The adapter to call. Injected rather than resolved here,
                so tests need no credentials and no vendor SDK.
            retry: Retry policy; the conservative default otherwise.
            sleep: Awaitable delay. Replaced in tests so they never wait.
            jitter: Returns a value in a range. Replaced in tests to make
                backoff deterministic.
        """
        self._provider = provider
        self._retry = retry or RetryPolicy()
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._jitter: Jitter = jitter or random.uniform

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMGateway:
        """Build the gateway the application is configured to use.

        Raises:
            LLMConfigurationError: The configured provider is unknown, or the
                selected provider has no API key.
        """
        # Imported here rather than at module scope so that importing the
        # gateway - to type-annotate a service, or to test it against a fake -
        # does not pull two vendor SDKs into the process.
        from app.ai.providers.registry import create_provider

        return cls(create_provider(settings), retry=RetryPolicy.from_settings(settings))

    @property
    def provider_name(self) -> str:
        """Which provider is serving calls."""
        return self._provider.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} provider={self.provider_name!r}>"

    # -- Public interface -----------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Produce a completion.

        The response is whatever the adapter returned, unchanged - including
        its ``latency_ms``, which measures the provider call rather than this
        operation.

        Raises:
            LLMError: The normalised failure, after any retries. The specific
                subclass is preserved; nothing is flattened.
        """
        return await self._attempt(
            lambda: self._provider.generate(request),
            request=request,
            operation="generate",
        )

    async def generate_structured[DataT: BaseModel](
        self,
        request: LLMRequest,
        schema: type[DataT],
    ) -> LLMStructuredResponse[DataT]:
        """Produce a completion parsed into *schema*.

        Structured output is entirely the adapter's business - each provider
        has its own mechanism. The gateway adds only selection, retries and
        observability, exactly as for :meth:`generate`.
        """
        return await self._attempt(
            lambda: self._provider.generate_structured(request, schema),
            request=request,
            operation="generate_structured",
        )

    # -- The retry loop -------------------------------------------------------

    async def _attempt[ResultT](
        self,
        call: Callable[[], Awaitable[ResultT]],
        *,
        request: LLMRequest,
        operation: str,
    ) -> ResultT:
        """Run *call*, retrying transient failures within the policy."""
        # Correlates every log line for this operation. The ambient HTTP
        # request id is attached by the log formatter already, so this only has
        # to distinguish calls made while serving one request.
        call_id = uuid.uuid4().hex[:16]
        started = perf_counter()

        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                result = await call()
            except LLMError as error:
                delay = self._delay_before_retry(attempt, error)
                if delay is None:
                    self._log_failure(call_id, request, operation, attempt, error, started)
                    raise

                self._log_retry(call_id, request, operation, attempt, error, delay)
                await self._sleep(delay)
            else:
                self._log_success(call_id, request, operation, attempt, result, started)
                return result

        # Unreachable: the final attempt either returns or raises above. Kept
        # so the function has no implicit None path.
        raise AssertionError("retry loop completed without a result")

    def _delay_before_retry(self, attempt: int, error: LLMError) -> float | None:
        """Seconds to wait before the next attempt, or None to give up.

        Giving up happens for three reasons: the error is not transient, the
        attempts are used up, or the provider asked us to wait longer than this
        deployment is willing to hold a caller.
        """
        if attempt >= self._retry.max_attempts or not is_retryable(error):
            return None

        ceiling = self._retry.backoff_ceiling(attempt)

        if isinstance(error, LLMRateLimitError) and error.retry_after_seconds is not None:
            hint = error.retry_after_seconds
            if hint > self._retry.max_delay_seconds:
                # Honour it by not retrying rather than by blocking: the caller
                # gets the error with retry_after intact and can decide.
                return None
            return max(hint, 0.0)

        # Full jitter: uniform in [0, ceiling]. Spreading retries out is what
        # stops a burst of failures becoming a synchronised thundering herd.
        return self._jitter(0.0, ceiling)

    # -- Observability --------------------------------------------------------
    #
    # Safe metadata only. Prompts and completions are never logged: they are
    # the tenant's business data. Credentials cannot appear because the gateway
    # never holds one - the adapter passes the key straight to its SDK client.

    def _context(
        self, call_id: str, request: LLMRequest, operation: str, attempt: int
    ) -> dict[str, Any]:
        return {
            "llm_call_id": call_id,
            "operation": operation,
            "provider": self.provider_name,
            "model": request.model,
            "attempt": attempt,
            "max_attempts": self._retry.max_attempts,
        }

    def _log_success(
        self,
        call_id: str,
        request: LLMRequest,
        operation: str,
        attempt: int,
        result: object,
        started: float,
    ) -> None:
        response = result.response if isinstance(result, LLMStructuredResponse) else result
        context = self._context(call_id, request, operation, attempt)
        context["outcome"] = "success"
        context["gateway_latency_ms"] = round((perf_counter() - started) * 1000, 2)

        if isinstance(response, LLMResponse):
            context["provider_latency_ms"] = round(response.latency_ms, 2)
            context["served_model"] = response.model
            context["input_tokens"] = response.usage.input_tokens
            context["output_tokens"] = response.usage.output_tokens
            context["request_id"] = response.request_id

        logger.info("LLM call succeeded", extra={"context": context})

    def _log_retry(
        self,
        call_id: str,
        request: LLMRequest,
        operation: str,
        attempt: int,
        error: LLMError,
        delay: float,
    ) -> None:
        context = self._context(call_id, request, operation, attempt)
        context["outcome"] = "retrying"
        context["error"] = type(error).__name__
        context["error_code"] = error.code
        context["delay_seconds"] = round(delay, 3)

        logger.warning("LLM call failed; retrying", extra={"context": context})

    def _log_failure(
        self,
        call_id: str,
        request: LLMRequest,
        operation: str,
        attempt: int,
        error: LLMError,
        started: float,
    ) -> None:
        context = self._context(call_id, request, operation, attempt)
        context["outcome"] = "failed"
        context["error"] = type(error).__name__
        context["error_code"] = error.code
        context["retryable"] = is_retryable(error)
        context["gateway_latency_ms"] = round((perf_counter() - started) * 1000, 2)

        logger.error("LLM call failed", extra={"context": context})
