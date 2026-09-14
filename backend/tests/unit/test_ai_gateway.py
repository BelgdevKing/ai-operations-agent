"""The LLM gateway.

Entirely offline: the provider is a fake, sleeping is injected, and no vendor
SDK, credential, database or socket is involved.
"""

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
from app.ai.gateway import (
    DEFAULT_MAX_RETRIES,
    LLMGateway,
    RetryPolicy,
    is_retryable,
)
from app.ai.models import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStructuredResponse,
    LLMUsage,
)
from app.ai.providers.base import LLMProvider
from app.core.config import Settings

PROMPT = "Summarise the pending refunds for Acme Ltd."
COMPLETION = "There are four pending refunds totalling GBP 812."
FAKE_KEY = "test-anthropic-secret"


class RefundSummary(BaseModel):
    pending: int
    currency: str


def a_request(**overrides: Any) -> LLMRequest:
    payload: dict[str, Any] = {
        "messages": [LLMMessage.user(PROMPT)],
        "model": "some-model",
    }
    payload.update(overrides)
    return LLMRequest(**payload)


def a_response(**overrides: Any) -> LLMResponse:
    payload: dict[str, Any] = {
        "content": COMPLETION,
        "provider": "fake",
        "model": "some-model-resolved",
        "usage": LLMUsage(input_tokens=120, output_tokens=34),
        "latency_ms": 250.0,
        "request_id": "req_provider_1",
    }
    payload.update(overrides)
    return LLMResponse(**payload)


class FakeProvider(LLMProvider):
    """A provider that returns, or fails, exactly as a test dictates."""

    name = "fake"

    def __init__(
        self,
        *,
        results: list[Any] | None = None,
        response: LLMResponse | None = None,
        structured: Any = None,
    ) -> None:
        # Each entry is returned, or raised if it is an exception.
        self.results = results
        self.response = response or a_response()
        self.structured = structured
        self.calls = 0
        self.structured_calls = 0
        self.last_schema: type[BaseModel] | None = None

    def _next(self) -> Any:
        if self.results is None:
            return self.response
        outcome = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        result = self._next()
        return result if isinstance(result, LLMResponse) else self.response

    async def generate_structured[DataT: BaseModel](
        self, request: LLMRequest, schema: type[DataT]
    ) -> LLMStructuredResponse[DataT]:
        self.calls += 1
        self.structured_calls += 1
        self.last_schema = schema
        self._next()
        data = self.structured or schema.model_validate({"pending": 4, "currency": "GBP"})
        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=data, response=self.response
        )


class RecordingSleeper:
    """Stands in for asyncio.sleep, so a test never actually waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def gateway(
    provider: FakeProvider | None = None,
    *,
    retry: RetryPolicy | None = None,
    sleeper: RecordingSleeper | None = None,
    jitter: Any = None,
) -> tuple[LLMGateway, FakeProvider, RecordingSleeper]:
    provider = provider or FakeProvider()
    sleeper = sleeper or RecordingSleeper()
    built = LLMGateway(
        provider,
        retry=retry,
        sleep=sleeper,
        # Deterministic by default: take the ceiling, so delays are predictable.
        jitter=jitter or (lambda _low, high: high),
    )
    return built, provider, sleeper


# -- Provider selection -------------------------------------------------------


def test_an_injected_provider_is_used() -> None:
    built, provider, _ = gateway()

    assert built.provider_name == provider.name


def test_the_configured_provider_is_selected() -> None:
    built = LLMGateway.from_settings(Settings(app_env="test", anthropic_api_key=FAKE_KEY))

    assert built.provider_name == "anthropic"


def test_switching_configuration_switches_provider() -> None:
    """No branch anywhere else in the application changes."""
    built = LLMGateway.from_settings(
        Settings(app_env="test", llm_provider="openai", openai_api_key="test-openai-secret")
    )

    assert built.provider_name == "openai"


def test_an_unknown_provider_raises_a_configuration_error() -> None:
    from pydantic import ValidationError

    # Configuration rejects it first; the registry is the second line.
    with pytest.raises(ValidationError):
        Settings(app_env="test", llm_provider="gemini")

    from app.ai.providers.registry import get_provider_class

    with pytest.raises(LLMConfigurationError):
        get_provider_class("gemini")


def test_a_missing_credential_raises_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError):
        LLMGateway.from_settings(Settings(app_env="test", llm_provider="openai"))


def test_settings_drive_the_retry_policy() -> None:
    policy = RetryPolicy.from_settings(
        Settings(app_env="test", llm_max_retries=5, llm_retry_max_delay_seconds=3.0)
    )

    assert policy.max_retries == 5
    assert policy.max_delay_seconds == 3.0


def test_the_gateway_repr_names_the_provider_and_no_secret() -> None:
    built, _, _ = gateway()

    assert "fake" in repr(built)
    assert FAKE_KEY not in repr(built)


# -- Success ------------------------------------------------------------------


async def test_generate_returns_the_completion() -> None:
    built, _, _ = gateway()

    response = await built.generate(a_request())

    assert response.content == COMPLETION


async def test_the_response_is_returned_unchanged() -> None:
    """The gateway adds no fields and rewrites none; its own metadata goes to
    the log instead."""
    original = a_response()
    built, _, _ = gateway(FakeProvider(response=original))

    response = await built.generate(a_request())

    assert response is original
    assert response.model_dump() == original.model_dump()


async def test_usage_is_preserved_not_recalculated() -> None:
    built, _, _ = gateway(
        FakeProvider(response=a_response(usage=LLMUsage(input_tokens=999, output_tokens=7)))
    )

    usage = (await built.generate(a_request())).usage

    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (999, 7, 1006)


async def test_the_served_model_is_preserved() -> None:
    built, _, _ = gateway(FakeProvider(response=a_response(model="actually-served")))

    assert (await built.generate(a_request())).model == "actually-served"


async def test_the_provider_request_id_is_preserved() -> None:
    built, _, _ = gateway(FakeProvider(response=a_response(request_id="req_from_provider")))

    assert (await built.generate(a_request())).request_id == "req_from_provider"


async def test_adapter_latency_is_not_overwritten() -> None:
    """latency_ms means the provider call; the gateway's own duration - which
    includes retries - is logged separately."""
    built, _, _ = gateway(FakeProvider(response=a_response(latency_ms=250.0)))

    assert (await built.generate(a_request())).latency_ms == 250.0


async def test_a_successful_first_attempt_does_not_retry() -> None:
    built, provider, sleeper = gateway()

    await built.generate(a_request())

    assert provider.calls == 1
    assert sleeper.delays == []


# -- Structured generation ----------------------------------------------------


async def test_generate_structured_delegates_to_the_provider() -> None:
    built, provider, _ = gateway()

    structured = await built.generate_structured(a_request(), RefundSummary)

    assert provider.structured_calls == 1
    assert provider.last_schema is RefundSummary
    assert isinstance(structured.data, RefundSummary)
    assert structured.data.pending == 4


async def test_generate_structured_preserves_usage_and_latency() -> None:
    built, _, _ = gateway()

    structured = await built.generate_structured(a_request(), RefundSummary)

    assert structured.response.usage.total_tokens == 154
    assert structured.response.latency_ms == 250.0


async def test_generate_structured_retries_the_same_way() -> None:
    provider = FakeProvider(results=[LLMTimeoutError(), None])
    built, provider, sleeper = gateway(provider)

    await built.generate_structured(a_request(), RefundSummary)

    assert provider.calls == 2
    assert len(sleeper.delays) == 1


# -- Retry classification -----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [LLMRateLimitError(), LLMTimeoutError(), LLMProviderError()],
)
def test_transient_failures_are_retryable(error: LLMError) -> None:
    assert is_retryable(error) is True


@pytest.mark.parametrize(
    "error",
    [LLMAuthenticationError(), LLMConfigurationError(), LLMInvalidResponseError()],
)
def test_permanent_failures_are_not_retryable(error: LLMError) -> None:
    """A wrong key, a wrong model name and output that missed the schema all
    fail identically on a second attempt."""
    assert is_retryable(error) is False


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (404, False), (422, False)])
def test_a_client_error_behind_a_provider_error_is_not_retried(
    status: int, retryable: bool
) -> None:
    """The adapters map a malformed request to LLMProviderError too; the status
    on the chained cause is what separates it from a transient fault."""

    class Cause(Exception):
        status_code = status

    error = LLMProviderError()
    error.__cause__ = Cause()

    assert is_retryable(error) is retryable


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 529])
def test_transient_statuses_behind_a_provider_error_are_retried(status: int) -> None:
    class Cause(Exception):
        status_code = status

    error = LLMProviderError()
    error.__cause__ = Cause()

    assert is_retryable(error) is True


def test_a_provider_error_with_no_status_is_retried() -> None:
    """Almost always a connection failure, which is worth another attempt."""
    assert is_retryable(LLMProviderError()) is True


# -- Retry behaviour ----------------------------------------------------------


@pytest.mark.parametrize("error", [LLMTimeoutError(), LLMRateLimitError(), LLMProviderError()])
async def test_a_transient_failure_is_retried_and_can_succeed(error: LLMError) -> None:
    built, provider, sleeper = gateway(FakeProvider(results=[error, a_response()]))

    response = await built.generate(a_request())

    assert provider.calls == 2
    assert len(sleeper.delays) == 1
    assert response.content == COMPLETION


@pytest.mark.parametrize(
    "error",
    [LLMAuthenticationError(), LLMConfigurationError(), LLMInvalidResponseError()],
)
async def test_a_permanent_failure_is_not_retried(error: LLMError) -> None:
    built, provider, sleeper = gateway(FakeProvider(results=[error]))

    with pytest.raises(type(error)):
        await built.generate(a_request())

    assert provider.calls == 1
    assert sleeper.delays == []


async def test_retries_are_bounded() -> None:
    built, provider, sleeper = gateway(FakeProvider(results=[LLMTimeoutError()]))

    with pytest.raises(LLMTimeoutError):
        await built.generate(a_request())

    assert provider.calls == RetryPolicy().max_attempts == DEFAULT_MAX_RETRIES + 1
    assert len(sleeper.delays) == DEFAULT_MAX_RETRIES


async def test_the_retry_count_is_configurable() -> None:
    built, provider, _ = gateway(
        FakeProvider(results=[LLMTimeoutError()]), retry=RetryPolicy(max_retries=4)
    )

    with pytest.raises(LLMTimeoutError):
        await built.generate(a_request())

    assert provider.calls == 5


async def test_retrying_can_be_disabled() -> None:
    built, provider, _ = gateway(
        FakeProvider(results=[LLMTimeoutError()]), retry=RetryPolicy(max_retries=0)
    )

    with pytest.raises(LLMTimeoutError):
        await built.generate(a_request())

    assert provider.calls == 1


async def test_the_final_normalized_error_is_propagated() -> None:
    """The specific subclass survives; nothing is flattened to LLMProviderError."""
    final = LLMRateLimitError("still limited", provider="fake")
    built, _, _ = gateway(FakeProvider(results=[final]))

    with pytest.raises(LLMRateLimitError) as raised:
        await built.generate(a_request())

    assert raised.value is final


async def test_no_real_sleeping_happens_in_tests() -> None:
    """The injected sleeper records instead of waiting."""
    built, _, sleeper = gateway(FakeProvider(results=[LLMTimeoutError()]))

    with pytest.raises(LLMTimeoutError):
        await built.generate(a_request())

    assert sleeper.delays  # recorded, never awaited for real


# -- Backoff ------------------------------------------------------------------


def test_the_backoff_ceiling_doubles_per_attempt() -> None:
    policy = RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=100.0)

    assert [policy.backoff_ceiling(n) for n in (1, 2, 3, 4)] == [0.5, 1.0, 2.0, 4.0]


def test_the_backoff_ceiling_is_capped() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=4.0)

    assert [policy.backoff_ceiling(n) for n in (1, 2, 3, 10)] == [1.0, 2.0, 4.0, 4.0]


async def test_delays_grow_between_attempts() -> None:
    built, _, sleeper = gateway(
        FakeProvider(results=[LLMTimeoutError()]),
        retry=RetryPolicy(max_retries=3, base_delay_seconds=0.5, max_delay_seconds=100.0),
    )

    with pytest.raises(LLMTimeoutError):
        await built.generate(a_request())

    assert sleeper.delays == [0.5, 1.0, 2.0]


async def test_jitter_never_exceeds_the_ceiling() -> None:
    """Real randomness this time, bounded by the configured maximum."""
    policy = RetryPolicy(max_retries=3, base_delay_seconds=1.0, max_delay_seconds=2.0)

    for _ in range(50):
        sleeper = RecordingSleeper()
        built = LLMGateway(FakeProvider(results=[LLMTimeoutError()]), retry=policy, sleep=sleeper)
        with pytest.raises(LLMTimeoutError):
            await built.generate(a_request())

        assert all(0.0 <= delay <= policy.max_delay_seconds for delay in sleeper.delays)


async def test_jitter_actually_varies() -> None:
    """Identical delays would resynchronise every client after an outage."""
    policy = RetryPolicy(max_retries=1, base_delay_seconds=4.0, max_delay_seconds=8.0)
    observed = set()

    for _ in range(30):
        sleeper = RecordingSleeper()
        built = LLMGateway(FakeProvider(results=[LLMTimeoutError()]), retry=policy, sleep=sleeper)
        with pytest.raises(LLMTimeoutError):
            await built.generate(a_request())
        observed.update(sleeper.delays)

    assert len(observed) > 1


# -- Rate limiting ------------------------------------------------------------


async def test_a_retry_after_hint_is_respected() -> None:
    built, _, sleeper = gateway(
        FakeProvider(results=[LLMRateLimitError(retry_after_seconds=3.0), a_response()]),
        retry=RetryPolicy(max_retries=2, base_delay_seconds=0.5, max_delay_seconds=8.0),
    )

    await built.generate(a_request())

    assert sleeper.delays == [3.0]


async def test_a_retry_after_hint_longer_than_the_ceiling_stops_retrying() -> None:
    """Honoured by not retrying rather than by holding the caller for a minute;
    the error keeps its hint so the caller can decide."""
    built, provider, sleeper = gateway(
        FakeProvider(results=[LLMRateLimitError(retry_after_seconds=600.0)]),
        retry=RetryPolicy(max_retries=2, max_delay_seconds=8.0),
    )

    with pytest.raises(LLMRateLimitError) as raised:
        await built.generate(a_request())

    assert provider.calls == 1
    assert sleeper.delays == []
    assert raised.value.retry_after_seconds == 600.0


async def test_rate_limiting_without_a_hint_uses_backoff() -> None:
    built, _, sleeper = gateway(
        FakeProvider(results=[LLMRateLimitError(), a_response()]),
        retry=RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=8.0),
    )

    await built.generate(a_request())

    assert sleeper.delays == [0.5]


# -- Observability ------------------------------------------------------------


async def test_a_successful_call_logs_safe_metadata(caplog: pytest.LogCaptureFixture) -> None:
    built, _, _ = gateway()

    with caplog.at_level(logging.INFO, logger="app.ai.gateway"):
        await built.generate(a_request())

    record = next(r for r in caplog.records if r.message == "LLM call succeeded")
    context = record.context  # type: ignore[attr-defined]

    assert context["provider"] == "fake"
    assert context["model"] == "some-model"
    assert context["served_model"] == "some-model-resolved"
    assert context["attempt"] == 1
    assert context["outcome"] == "success"
    assert context["input_tokens"] == 120
    assert context["output_tokens"] == 34
    assert context["provider_latency_ms"] == 250.0
    assert context["gateway_latency_ms"] >= 0
    assert context["llm_call_id"]


async def test_each_call_gets_its_own_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built, _, _ = gateway()

    with caplog.at_level(logging.INFO, logger="app.ai.gateway"):
        await built.generate(a_request())
        await built.generate(a_request())

    ids = [r.context["llm_call_id"] for r in caplog.records]  # type: ignore[attr-defined]

    assert len(set(ids)) == 2


async def test_every_attempt_of_one_call_shares_its_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built, _, _ = gateway(FakeProvider(results=[LLMTimeoutError()]))

    with caplog.at_level(logging.DEBUG, logger="app.ai.gateway"):
        with pytest.raises(LLMTimeoutError):
            await built.generate(a_request())

    ids = {r.context["llm_call_id"] for r in caplog.records}  # type: ignore[attr-defined]

    assert len(ids) == 1


async def test_a_retry_is_logged_with_its_delay(caplog: pytest.LogCaptureFixture) -> None:
    built, _, _ = gateway(FakeProvider(results=[LLMTimeoutError(), a_response()]))

    with caplog.at_level(logging.WARNING, logger="app.ai.gateway"):
        await built.generate(a_request())

    record = next(r for r in caplog.records if r.message == "LLM call failed; retrying")
    context = record.context  # type: ignore[attr-defined]

    assert context["outcome"] == "retrying"
    assert context["error"] == "LLMTimeoutError"
    assert context["attempt"] == 1
    assert context["delay_seconds"] >= 0


async def test_a_final_failure_is_logged_with_its_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built, _, _ = gateway(FakeProvider(results=[LLMAuthenticationError()]))

    with caplog.at_level(logging.ERROR, logger="app.ai.gateway"):
        with pytest.raises(LLMAuthenticationError):
            await built.generate(a_request())

    record = next(r for r in caplog.records if r.message == "LLM call failed")
    context = record.context  # type: ignore[attr-defined]

    assert context["outcome"] == "failed"
    assert context["error"] == "LLMAuthenticationError"
    assert context["error_code"] == "llm_authentication_error"
    assert context["retryable"] is False


async def test_gateway_latency_covers_retries(caplog: pytest.LogCaptureFixture) -> None:
    built, _, _ = gateway(FakeProvider(results=[LLMTimeoutError(), a_response()]))

    with caplog.at_level(logging.INFO, logger="app.ai.gateway"):
        await built.generate(a_request())

    record = next(r for r in caplog.records if r.message == "LLM call succeeded")

    assert record.context["attempt"] == 2  # type: ignore[attr-defined]
    assert record.context["gateway_latency_ms"] >= 0  # type: ignore[attr-defined]


# -- Nothing sensitive is logged ----------------------------------------------


async def test_the_prompt_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Prompts are the tenant's business data, not operational metadata."""
    built, _, _ = gateway()

    with caplog.at_level(logging.DEBUG):
        await built.generate(a_request())

    assert PROMPT not in caplog.text


async def test_the_completion_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    built, _, _ = gateway()

    with caplog.at_level(logging.DEBUG):
        await built.generate(a_request())

    assert COMPLETION not in caplog.text


async def test_prompt_and_completion_stay_out_of_failure_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built, _, _ = gateway(FakeProvider(results=[LLMTimeoutError()]))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMTimeoutError):
            await built.generate(a_request())

    assert PROMPT not in caplog.text
    assert COMPLETION not in caplog.text


def real_adapter_that_fails() -> Any:
    """A genuine AnthropicProvider holding a fake key, with its SDK client
    replaced so nothing leaves the process.

    Worth using the real adapter rather than the fake provider here: the point
    is that a key handed to an adapter cannot resurface through the gateway.
    """
    from app.ai.providers.anthropic import AnthropicProvider
    from tests.unit.ai_doubles import anthropic_client, anthropic_errors

    return AnthropicProvider(
        FAKE_KEY,
        timeout_seconds=30.0,
        client=anthropic_client(create_error=anthropic_errors()["authentication"]),
    )


async def test_a_credential_cannot_reach_a_gateway_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structural: the gateway never receives a key - the adapter hands it
    straight to its SDK client."""
    built = LLMGateway(real_adapter_that_fails(), retry=RetryPolicy(max_retries=0))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError):
            await built.generate(a_request(model="claude-opus-5"))

    assert caplog.text
    assert FAKE_KEY not in caplog.text


async def test_a_credential_cannot_reach_a_gateway_exception() -> None:
    built = LLMGateway(real_adapter_that_fails(), retry=RetryPolicy(max_retries=0))

    with pytest.raises(LLMError) as raised:
        await built.generate(a_request(model="claude-opus-5"))

    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in repr(raised.value)
    assert FAKE_KEY not in str(raised.value.details)


async def test_a_credential_cannot_reach_a_response() -> None:
    built, _, _ = gateway()

    response = await built.generate(a_request())

    assert FAKE_KEY not in str(response.model_dump())
