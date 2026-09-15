"""Recording what a direct model call used.

``AIService`` sits between an HTTP request and the gateway and has deliberately
never held a database session - it decides what the *application* permits and
leaves everything else to the layers around it. Handing it a repository would
undo that, so it is handed a **protocol** instead, the same arrangement
``AgentRunner`` has with :class:`app.agents.journal.RunJournal`: the service
calls one method, and whether that method writes to PostgreSQL or does nothing
is somebody else's decision.

What is recorded is metadata and only metadata - model, token counts, latency,
and the correlation id of the request. Not the prompt, not the completion, not
an idempotency key. The record exists so a tenant's usage adds up, and a usage
row is read by more people than the conversation it came from.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import LLMResponse
from app.repositories.generation import GenerationUsageRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationRecord:
    """One direct model call, in the terms a usage ledger needs.

    Built from an :class:`LLMResponse`, which is provider-independent - so this
    carries the model that answered and never which vendor it was.
    """

    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None = None

    @classmethod
    def from_response(cls, response: LLMResponse, *, request_id: str | None) -> GenerationRecord:
        return cls(
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=round(response.latency_ms),
            request_id=request_id,
        )


class GenerationRecorder(Protocol):
    """Somewhere for a direct generation's usage to go."""

    async def record(self, record: GenerationRecord, *, user_id: uuid.UUID) -> None:
        """Write down one call. The organization is the recorder's own."""
        ...


class NullGenerationRecorder:
    """A recorder that keeps nothing.

    The default, so ``AIService`` can be built with no database at all - which
    is what the unit suite does. Silent rather than raising: a deployment that
    is not accounting for usage is a configuration, not an error.
    """

    async def record(self, record: GenerationRecord, *, user_id: uuid.UUID) -> None:
        del record, user_id


NULL_RECORDER: GenerationRecorder = NullGenerationRecorder()
"""The recorder used when nothing is being persisted."""


class DatabaseGenerationRecorder:
    """Writes direct-generation usage to PostgreSQL, for one organization.

    Bound to an organization when it is built, from a verified membership. The
    ``record`` method has no organization parameter and could not take one: this
    is the same shape every other tenant-scoped writer in the codebase has, and
    it is what makes "usage cannot be attributed to the wrong tenant" a property
    of the constructor rather than of every call site.
    """

    def __init__(self, session: AsyncSession, organization_id: uuid.UUID) -> None:
        self._session = session
        self._usage = GenerationUsageRepository(session, organization_id)
        self._organization_id = organization_id

    async def record(self, record: GenerationRecord, *, user_id: uuid.UUID) -> None:
        await self._usage.record(
            user_id=user_id,
            model=record.model,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            latency_ms=record.latency_ms,
            request_id=record.request_id,
        )

        logger.info(
            "Direct generation recorded",
            extra={
                "context": {
                    "organization_id": str(self._organization_id),
                    "user_id": str(user_id),
                    "model": record.model,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                }
            },
        )
