"""Agents, the tool registry, and the link between them.

Nothing here executes anything: these are the definitions an agent runtime will
later read. No Claude client, no tool handlers, no execution loop.

Agents are tenant-owned. Tools are not: the registry is platform-level, so that
a tool's schema, handler and sensitivity are defined once and audited once.
Which tools a given agent may use is the tenant-specific part, and that is what
``agent_tools`` records.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import (
    CreatedAtMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.organization import Organization


class Agent(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """An agent definition belonging to one organization."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The system prompt. Text rather than a bounded string: prompts grow.
    system_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Claude model identifier, for example "claude-sonnet-5".
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    organization: Mapped[Organization] = relationship()

    tools: Mapped[list[Tool]] = relationship(
        secondary="agent_tools",
        back_populates="agents",
        passive_deletes=True,
    )

    __table_args__ = (
        # Agent names are how people refer to them, so they must be
        # unambiguous inside an organization - but not across organizations.
        UniqueConstraint("organization_id", "name", name="uq_agents_organization_id_name"),
        Index("ix_agents_organization_id_enabled", "organization_id", "enabled"),
    )


class Tool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A capability an agent may invoke.

    Platform-level, so ``name`` is globally unique. ``requires_approval`` is the
    switch that makes a tool call pause for a human rather than execute; the
    gate that reads it is implemented in the approvals phase.
    """

    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    agents: Mapped[list[Agent]] = relationship(
        secondary="agent_tools",
        back_populates="tools",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_tools_enabled", "enabled"),)


class AgentTool(CreatedAtMixin, Base):
    """Which tools an agent is permitted to use.

    A mapped class rather than a bare table so the grant can carry its own
    columns - ``created_at`` now, and whatever the permission model needs
    later. The composite primary key makes a duplicate grant impossible.
    """

    __tablename__ = "agent_tools"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="CASCADE"),
        primary_key=True,
    )

    __table_args__ = (
        # The composite primary key already indexes (agent_id, tool_id); this
        # covers the reverse question, "which agents use this tool?".
        Index("ix_agent_tools_tool_id", "tool_id"),
    )
