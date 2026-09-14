"""Errors raised by the AI abstraction.

Every provider SDK has its own exception hierarchy. None of them may escape
this package: an adapter catches what its SDK raises and re-raises one of these,
chaining the original with ``raise ... from exc`` so the cause survives in the
logs without becoming part of the application's type surface. Without that rule,
catching "an LLM failure" anywhere else would mean importing every SDK.

These extend ``AppError``, so a failure that reaches the API layer is rendered
in the standard error envelope with a correlation id rather than surfacing as an
unhandled 500.

**Status codes describe our situation, not the provider's.** A provider
rejecting our API key is a misconfiguration on this side, so it is a 500 - not
the 401 the provider returned, which in this application means "your access
token is bad" and would send a signed-in user to the login screen because of a
deployment mistake.

Messages are the other half of that: an ``AppError`` message can be returned to
a client, so the defaults here say what happened without quoting the provider.
Raw provider text may name models, quotas or internal endpoints, and belongs in
the log with the chained cause.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import AppError


class LLMError(AppError):
    """Base class for every failure inside the AI abstraction.

    502 by default: the request was well-formed and the caller is entitled to
    make it, but something upstream did not deliver.
    """

    status_code = 502
    code = "llm_error"
    message = "The language model request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Kept off ``details``, which is serialised to clients. Which vendor
        # serves a request is internal architecture, useful in a log line and
        # not something an API response needs to disclose.
        self.provider = provider
        super().__init__(message, code=code, details=details)

    def __str__(self) -> str:
        return f"[{self.provider}] {self.message}" if self.provider else self.message


class LLMConfigurationError(LLMError):
    """The provider is not usable as configured - no API key, unknown provider
    name, a model the deployment does not allow.

    500, because nothing the caller did caused it and nothing they can do will
    fix it.
    """

    status_code = 500
    code = "llm_configuration_error"
    message = "The language model provider is not configured correctly."


class LLMAuthenticationError(LLMError):
    """The provider rejected our credentials.

    Deliberately 500 and not 401. The caller authenticated with this platform
    perfectly well; it is our key that is missing, expired or revoked.
    """

    status_code = 500
    code = "llm_authentication_error"
    message = "The language model provider rejected the platform's credentials."


class LLMRateLimitError(LLMError):
    """The provider is throttling us.

    Carries ``retry_after_seconds`` when the provider says how long to wait, so
    a caller can back off instead of guessing.
    """

    status_code = 429
    code = "llm_rate_limited"
    message = "The language model provider is rate limiting requests. Try again shortly."

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message, provider=provider, code=code, details=details)


class LLMTimeoutError(LLMError):
    """The provider did not answer in time.

    504 rather than 408: the timeout is between this service and the provider,
    not between the client and this service.
    """

    status_code = 504
    code = "llm_timeout"
    message = "The language model provider did not respond in time."


class LLMProviderError(LLMError):
    """The provider returned an error we have no more specific class for -
    a server fault, an overloaded model, an unexpected status."""

    status_code = 502
    code = "llm_provider_error"
    message = "The language model provider returned an error."


class LLMInvalidResponseError(LLMError):
    """The call succeeded but the result was unusable.

    Raised when a response cannot be parsed, or when structured output fails
    validation against the requested schema. Distinct from
    :class:`LLMProviderError` because the remedy differs: this one usually means
    the prompt or schema needs work, not that the provider is unwell.
    """

    status_code = 502
    code = "llm_invalid_response"
    message = "The language model returned a response that could not be used."
