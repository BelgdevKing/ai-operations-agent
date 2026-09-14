"""Resolving a provider name to an adapter.

Deliberately a lookup and a constructor, not a framework. Its only job is to
turn ``"anthropic"`` into a configured :class:`AnthropicProvider`, so that
provider selection happens in exactly one place. No endpoint, service or
repository should ever branch on which vendor is in use.

The gateway that adds retries, fallback and per-tenant routing is a later
stage; this is what it will build on.
"""

from __future__ import annotations

from typing import Final

from app.ai.exceptions import LLMConfigurationError
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import LLMProvider
from app.ai.providers.openai import OpenAIProvider
from app.core.config import Settings

PROVIDERS: Final[dict[str, type[LLMProvider]]] = {
    AnthropicProvider.name: AnthropicProvider,
    OpenAIProvider.name: OpenAIProvider,
}


def available_providers() -> tuple[str, ...]:
    """Names that :func:`get_provider_class` will resolve."""
    return tuple(sorted(PROVIDERS))


def get_provider_class(name: str) -> type[LLMProvider]:
    """Look up an adapter class by name.

    Raises:
        LLMConfigurationError: The name is not a provider this build supports.
            A configuration error rather than a lookup failure, because the
            only way to reach it is a bad ``LLM_PROVIDER`` value.
    """
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise LLMConfigurationError(
            f"Unknown language model provider {name!r}. "
            f"Available: {', '.join(available_providers())}."
        ) from exc


def create_provider(settings: Settings) -> LLMProvider:
    """Build the adapter the application is configured to use.

    The credential is read from settings at the moment of construction and
    handed straight to the SDK client. It is not stored on the adapter, logged,
    or returned anywhere.

    Raises:
        LLMConfigurationError: The provider is unknown, or the selected
            provider has no API key. Development deliberately starts without
            one - see ``Settings`` - so this is where that turns into a clear
            failure instead of a confusing call to an unauthenticated client.
    """
    provider_class = get_provider_class(settings.llm_provider)

    api_key = settings.llm_api_key
    if api_key is None:
        raise LLMConfigurationError(
            f"LLM_PROVIDER is {settings.llm_provider!r} but "
            f"{settings.llm_provider.upper()}_API_KEY is not set."
        )

    return provider_class(  # type: ignore[call-arg]
        api_key=api_key.get_secret_value(),
        timeout_seconds=settings.llm_timeout_seconds,
    )
