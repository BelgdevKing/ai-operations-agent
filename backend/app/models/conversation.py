"""Conversations and their messages.

Storage only: nothing here calls a model or streams a reply.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text
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
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
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
    agent: Mapped[Agent] = relationship()

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # Tie-broken by id: created_at defaults to now(), which PostgreSQL
        # holds constant for a whole transaction, so two messages written
        # together would otherwise come back in an arbitrary order.
        order_by="Message.created_at, Message.id",
    )

    __table_args__ = (
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
    role: Mapped[MessageRole] = mapped_column(
        enum_column(MessageRole, "role"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        # Replaying a conversation reads it in order; one composite index
        # serves both the filter and the sort.
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )
