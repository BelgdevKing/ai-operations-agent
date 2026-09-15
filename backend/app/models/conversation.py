"""Conversations and their messages.

Storage. Nothing here calls a model or streams a reply - the agent runtime
writes through a repository, and this is what it writes to.

**This is the content store.** A persisted agent conversation contains the
user's questions, the agent's answers, the tool requests it made and the tool
results it was given. Business information returned by a tool therefore lives
here, by design: the operational tables (``agent_runs``, ``agent_steps``,
``tool_executions``) deliberately do not duplicate it. Access to a conversation
is consequently tenant-scoped and subject to the same authentication and
authorization as every other tenant-owned resource.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ConversationStatus, MessageRole, enum_column
from app.models.mixins import (
    CreatedAtMixin,
    OrganizationScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.organization import Organization
    from app.models.user import User


class Conversation(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """An exchange between one user and one agent."""

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        # RESTRICT, not CASCADE: a person leaving must not erase the record of
        # what was asked and done.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Nullable since the durable-execution phase. An agent definition comes
    # from the server-side registry rather than this table, and the platform
    # agent every tenant can run has no owning organization - which
    # ``agents.organization_id NOT NULL`` cannot represent. A conversation served
    # by such an agent records no row here; which agent actually ran is on the
    # run (``agent_runs.agent_id``), and a tenant-owned agent still links.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Nullable: titles are generated from the first exchange, so a conversation
    # exists briefly without one.
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[ConversationStatus] = mapped_column(
        enum_column(ConversationStatus, "status"),
        nullable=False,
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE.value,
    )

    organization: Mapped[Organization] = relationship()
    user: Mapped[User] = relationship()
    agent: Mapped[Agent | None] = relationship()

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # By position, which is the only total order a conversation has.
        order_by="Message.sequence, Message.created_at, Message.id",
    )

    __table_args__ = (
        # Referenced by ``agent_runs``'s composite foreign key, so a run cannot
        # be attached to another tenant's conversation.
        UniqueConstraint("id", "organization_id", name="uq_conversations_id_organization_id"),
        Index("ix_conversations_user_id", "user_id"),
        Index("ix_conversations_agent_id", "agent_id"),
        Index("ix_conversations_organization_id_status", "organization_id", "status"),
        # The conversation list is per organization, newest first.
        Index("ix_conversations_organization_id_created_at", "organization_id", "created_at"),
    )


class Message(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One turn in a conversation.

    Immutable, so it carries only ``created_at``. Not organization-scoped: a
    message reaches its tenant through its conversation, and duplicating
    ``organization_id`` here would create a second source of truth that could
    disagree with the first.
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Position in the conversation, from 1. The order a conversation is
    # replayed in has to be a total order, and ``created_at`` is not one:
    # PostgreSQL holds ``now()`` constant for a whole transaction, so every turn
    # written together shares a timestamp and the tie-break falls to a random
    # primary key. Replaying a transcript in an arbitrary order would change what
    # the model was asked.
    #
    # Not unique, deliberately. A conversation is advanced by one run at a time -
    # the idempotency key sees to that - and a constraint here would turn a
    # genuine concurrent write into a 500 rather than an interleaving nobody
    # would otherwise notice.
    sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    role: Mapped[MessageRole] = mapped_column(
        enum_column(MessageRole, "role"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # The structured form of a turn the runtime rendered, so a stored
    # conversation can be replayed into the model without parsing its own text
    # back out. Present on exactly two kinds of turn:
    #
    #   assistant tool request  {"type": "tool_request", "tool_name", "arguments",
    #                            "tool_execution_id"}
    #   tool result             {"type": "tool_result", "tool_name", "execution_id",
    #                            "succeeded", "outcome"}
    #
    # This is the content store, so the arguments are allowed here - they are
    # already in ``content``, which is the rendered text of the same turn. They
    # are *not* allowed in ``tool_executions`` or in ``approvals.parameters``.
    tool_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        # Replaying a conversation reads it in order; one composite index
        # serves both the filter and the sort.
        Index("ix_messages_conversation_id_sequence", "conversation_id", "sequence"),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )
