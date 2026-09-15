"""Data access for direct-generation usage.

One write and nothing else. The rows are immutable - a call happened, it used
these tokens, it took this long - so there is no update, no claim and no
conditional transition here, and the file is short because the table is boring
by design.

Tenant-scoped through :class:`TenantScopedRepository`, so the organization comes
from the caller's verified membership and is applied in the one place the whole
codebase applies it.
"""

from __future__ import annotations

import uuid

from app.models.generation import GenerationUsage
from app.repositories.tenant import TenantScopedRepository


class GenerationUsageRepository(TenantScopedRepository[GenerationUsage]):
    """Direct-generation usage for one organization."""

    model = GenerationUsage

    async def record(
        self,
        *,
        user_id: uuid.UUID,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        request_id: str | None = None,
    ) -> GenerationUsage:
        """Write down one direct model call.

        The organization is not a parameter. It comes from the repository, which
        was built from a verified membership - so there is no argument here that
        a request body could reach, and no way to attribute usage to a tenant
        other than the caller's own.

        Flushed rather than committed: the request's own transaction decides
        whether this survives, so a usage row cannot outlive a response that
        never reached the caller.
        """
        usage = GenerationUsage(
            organization_id=self.organization_id,
            user_id=user_id,
            request_id=request_id,
            model=model,
            input_tokens=max(input_tokens, 0),
            output_tokens=max(output_tokens, 0),
            latency_ms=max(latency_ms, 0),
        )
        self.session.add(usage)
        await self.session.flush()
        return usage
