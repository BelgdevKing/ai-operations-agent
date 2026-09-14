"""Application-level AI service.

The layer between an HTTP request and the gateway. It decides what the
*application* permits - which model may be asked for, how large a prompt may be,
what the defaults are - and leaves everything operational to the gateway.

    endpoint  ->  AIService  ->  LLMGateway  ->  LLMProvider  ->  vendor

What it deliberately does not do: pick a provider, retry, translate provider
errors, or touch an SDK. Those belong below it, and duplicating them here is
how a second, subtly different policy gets born.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from app.ai.exceptions import LLMConfigurationError
from app.ai.gateway import LLMGateway
from app.ai.models import LLMMessage, LLMRequest, LLMResponse, LLMStructuredResponse
from app.core.config import Settings
from app.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class AIService:
    """Generates completions on behalf of the application."""

    def __init__(self, gateway: LLMGateway, settings: Settings) -> None:
        self._gateway = gateway
        self._settings = settings

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> LLMResponse:
        """Ask the configured model for a completion.

        Args:
            messages: Conversation, already provider-independent.
            model: Optional. The deployment's configured model is used when
                omitted; a named one must be allowed.
            temperature: Optional. Left unset so the provider's own default
                applies - which also avoids sending a sampling parameter to a
                model that rejects one.
            max_output_tokens: Optional cap on the generated response.
            organization_id: The tenant this call is on behalf of. Recorded for
                attribution only; it comes from the caller's verified
                membership, never from a request body.

        Raises:
            ValidationError: The requested model is not permitted.
            LLMError: Any failure from the gateway, already normalised.
        """
        request = self._build_request(
            messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

        # Tenant attribution for the log only. No prompt or completion text: the
        # gateway logs operational metadata and this adds who it was for.
        logger.info(
            "AI generation requested",
            extra={
                "context": {
                    "organization_id": str(organization_id) if organization_id else None,
                    "model": request.model,
                    "messages": len(request.messages),
                }
            },
        )

        return await self._gateway.generate(request)

    async def generate_structured[DataT: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        schema: type[DataT],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMStructuredResponse[DataT]:
        """Ask for a completion parsed into *schema*.

        Not exposed over HTTP yet; present so that services needing structured
        output go through the same validation as everything else rather than
        reaching for the gateway directly.
        """
        request = self._build_request(
            messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        return await self._gateway.generate_structured(request, schema)

    # -- Application policy ---------------------------------------------------

    def _build_request(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None,
        temperature: float | None,
        max_output_tokens: int | None,
    ) -> LLMRequest:
        """Turn application input into a gateway request.

        Optional parameters are omitted rather than defaulted, so the internal
        model's own defaults apply and an adapter can still tell "the caller
        did not ask" from "the caller asked for this".
        """
        if not messages:
            raise ValidationError("At least one message is required.")

        overrides: dict[str, object] = {}
        if temperature is not None:
            overrides["temperature"] = temperature
        if max_output_tokens is not None:
            overrides["max_output_tokens"] = max_output_tokens

        return LLMRequest(
            messages=list(messages),
            model=self.resolve_model(model),
            **overrides,
        )

    def resolve_model(self, requested: str | None) -> str:
        """Decide which model to call, with the server having the last word.

        Omitting the model is the normal path and gives the configured one.
        Naming one only works if the deployment listed it, so a caller cannot
        direct traffic at an expensive model - or at a different provider's
        model, which would fail at the adapter anyway.
        """
        configured = self._settings.llm_model
        if requested is None:
            if not configured:
                raise LLMConfigurationError("No model is configured for this deployment.")
            return configured

        if requested not in self._settings.allowed_models:
            # Says what is permitted rather than merely refusing: the allowed
            # set is deployment configuration, not a secret.
            allowed = ", ".join(sorted(self._settings.allowed_models))
            raise ValidationError(
                f"Model {requested!r} is not available. Allowed models: {allowed}."
            )

        return requested
