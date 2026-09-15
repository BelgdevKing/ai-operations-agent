"""Usage from direct generation, outside the durable agent lifecycle.

``/ai/generate`` asks the configured model a question and returns the answer.
It creates no agent run, no conversation and no tool execution, because it is
not an agent - which left its tokens invisible to every usage query, since
those read ``agent_steps``.

**Why this is its own table rather than a reused one.** The obvious reuse is to
write an ``agent_runs`` row and an ``agent_steps`` row for each direct
generation. That was rejected: ``agent_steps.run_id`` is ``NOT NULL`` with a
composite key into ``agent_runs``, so the reuse forces a run into existence -
with a fabricated ``agent_id``, no conversation to open, and a place in
``GET /ai/runs`` where it would appear to a user as a run they cannot read. It
would mean changing what a Part 16 agent run *is* in order to make a Part 19
report add up, and a record that lies about its own kind is worse than a second
small table.

**Nine columns, all operational.** No prompt, no completion, no arguments, no
tool results, no conversation, no idempotency key, no credential. The same rule
``agent_steps`` follows, for the same reason: what was asked and what came back
are the tenant's business data, and this is the record an operator needs to
explain a bill.

**No provider column**, deliberately, matching ``agent_steps.model``: which
vendor answered is internal routing and is absent from every outward contract.
Cost is keyed on the model, which is the dimension a price book uses anyway.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import CreatedAtMixin, OrganizationScopedMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User

MODEL_NAME_MAX_LENGTH = 100
REQUEST_ID_MAX_LENGTH = 64


class GenerationUsage(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """One direct model call, recorded for usage accounting.

    Immutable - written once when the call returns, never updated - so it
    carries only ``created_at``. That timestamp is also the aggregation key:
    the index below is the one a usage report over a window actually uses.
    """

    __tablename__ = "generation_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT: who spent the tokens is part of the record, and a usage
        # history that loses its attribution is not much of a history.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The HTTP correlation id, so one figure can be traced back to the request
    # that produced it. Correlation only: nothing is keyed on it, nothing is
    # deduplicated by it, and it is not an idempotency key.
    request_id: Mapped[str | None] = mapped_column(String(REQUEST_ID_MAX_LENGTH), nullable=True)

    # The model that served the call. Not the provider - see the module
    # docstring - and the dimension the price book is keyed on.
    model: Mapped[str] = mapped_column(String(MODEL_NAME_MAX_LENGTH), nullable=False)

    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # Measured by the gateway rather than reported by the provider, so it is
    # always present - the same figure ``agent_steps.latency_ms`` carries.
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    organization: Mapped[Organization] = relationship(viewonly=True)
    user: Mapped[User] = relationship(viewonly=True)

    __table_args__ = (
        # Redundant on its own - ``id`` is already unique - and present for the
        # Part 14 reason: it is what a future table would need to reference this
        # one with a tenant-carrying composite key. Adding it now costs an index
        # and saves a migration later.
        UniqueConstraint("id", "organization_id", name="uq_generation_usage_id_organization_id"),
        # The aggregation index. Every usage query is "this tenant, this
        # window", which is exactly this pair, in this order.
        Index("ix_generation_usage_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_generation_usage_organization_id_model", "organization_id", "model"),
        Index("ix_generation_usage_user_id", "user_id"),
    )
