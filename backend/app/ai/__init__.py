"""Provider-independent access to large language models.

The application talks to this package; this package talks to vendors. Nothing
outside it imports an Anthropic or OpenAI SDK, so changing provider, adding a
second one, or running against a fake in tests is a change here rather than
everywhere a model is called.

Being built in stages. Present now: the shared types, the error hierarchy, the
provider interface, the :class:`LLMGateway` the application calls, and -
under ``app.ai.providers`` - the Anthropic and OpenAI adapters behind a small
registry. Still to come: the service layer above the gateway.

Application code uses ``LLMGateway`` and never constructs an adapter itself.

Importing this package deliberately does **not** import a vendor SDK. The
adapters live one level down so that code needing only the shared types does
not pay for two SDKs it will not call; a test enforces it.
"""

from __future__ import annotations

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.gateway import LLMGateway, RetryPolicy, is_retryable
from app.ai.models import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    LLMStructuredResponse,
    LLMUsage,
)
from app.ai.providers.base import LLMProvider

__all__ = [
    "LLMAuthenticationError",
    "LLMConfigurationError",
    "LLMError",
    "LLMGateway",
    "LLMInvalidResponseError",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "LLMStructuredResponse",
    "LLMTimeoutError",
    "LLMUsage",
    "RetryPolicy",
    "is_retryable",
]
