"""Data access for stored conversations.

The content store. Everything a run said, was told, asked a tool for and got
back lives here - which is precisely why it is tenant-scoped at the repository
and why ``messages`` is only ever reached through its conversation.

``Message`` carries no ``organization_id`` of its own, by the original schema's
design: a message reaches its tenant through its conversation, and a second copy
of the column could disagree with the first. So every read here starts from a
conversation this organization owns, and there is no method that takes a message
id on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.agents.journal import (
    TURN_TOOL_REQUEST,
    TURN_TOOL_RESULT,
    tool_result_from_payload,
)
from app.ai.models import LLMMessage, LLMRole
from app.models.conversation import Conversation, Message
from app.models.enums import ConversationStatus, MessageRole
from app.repositories.tenant import TenantScopedRepository

# How many turns one conversation may be replayed from. The runtime bounds the
# conversation it sends a model; this bounds what is read back before it gets
# there, so a pathological row count cannot turn one request into a table scan.
MAX_REPLAYED_MESSAGES = 500

_ROLE_TO_LLM: dict[MessageRole, LLMRole] = {
    MessageRole.USER: LLMRole.USER,
    MessageRole.ASSISTANT: LLMRole.ASSISTANT,
    MessageRole.SYSTEM: LLMRole.SYSTEM,
    MessageRole.TOOL: LLMRole.TOOL,
}

_LLM_TO_ROLE: dict[LLMRole, MessageRole] = {
    LLMRole.USER: MessageRole.USER,
    LLMRole.ASSISTANT: MessageRole.ASSISTANT,
    LLMRole.SYSTEM: MessageRole.SYSTEM,
    LLMRole.TOOL: MessageRole.TOOL,
}


class ConversationRepository(TenantScopedRepository[Conversation]):
    """Conversations of one organization."""

    model = Conversation

    async def create(self, *, user_id: uuid.UUID, title: str | None = None) -> Conversation:
        """Start a conversation owned by this organization.

        ``agent_id`` is deliberately left unset. Agent definitions come from the
        server-side registry rather than the ``agents`` table, and the platform
        agent every tenant can run has no row there to point at - see
        ``app.models.conversation``. Which agent served a conversation is on the
        run (``agent_runs.agent_id``), which is where it is actually true.
        """
        conversation = Conversation(user_id=user_id, title=title)
        self.add(conversation)
        await self.session.flush()
        return conversation

    async def get_active(self, conversation_id: uuid.UUID) -> Conversation | None:
        """One of this organization's conversations, if it is still usable.

        An archived or deleted conversation answers the same as one belonging to
        another tenant: absent. A caller cannot tell those apart, which is the
        same reasoning every other scoped lookup uses.
        """
        statement = self.select().where(
            Conversation.id == conversation_id,
            Conversation.status == ConversationStatus.ACTIVE,
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def list_recent(self, *, limit: int = 20) -> Sequence[Conversation]:
        """This organization's conversations, newest first."""
        statement = (
            self.select()
            .where(Conversation.status == ConversationStatus.ACTIVE)
            .order_by(Conversation.created_at.desc(), Conversation.id)
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def with_messages(self, conversation_id: uuid.UUID) -> Conversation | None:
        """One conversation and its turns, in order."""
        statement = (
            self.select()
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages))
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def messages(self, conversation_id: uuid.UUID) -> Sequence[Message]:
        """The turns of one of this organization's conversations, oldest first.

        Scoped by joining back to the conversation rather than by trusting the
        id: ``messages`` has no tenant column, so the only safe way to read one
        is through a conversation this organization owns.
        """
        statement = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.organization_id == self.organization_id,
            )
            .order_by(Message.sequence, Message.created_at, Message.id)
            .limit(MAX_REPLAYED_MESSAGES)
        )
        result = await self.session.execute(statement)
        return result.scalars().all()

    async def next_sequence(self, conversation_id: uuid.UUID) -> int:
        """The position the next turn takes, from 1."""
        statement = (
            select(func.coalesce(func.max(Message.sequence), 0) + 1)
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.organization_id == self.organization_id,
            )
        )
        result = await self.session.execute(statement)
        return int(result.scalar_one())

    async def append_all(
        self,
        conversation_id: uuid.UUID,
        turns: Iterable[tuple[LLMMessage, dict[str, object] | None]],
    ) -> list[Message]:
        """Stage turns in order, numbered from wherever the conversation got to.

        The position is read once per batch rather than held between calls: a
        counter carried across a run would be wrong the moment anything else
        wrote to the same conversation, and one extra ``SELECT max(sequence)``
        per write is not a cost worth that risk.

        Staged, not committed. The caller decides the transaction boundary.
        """
        staged: list[Message] = []
        position = await self.next_sequence(conversation_id)

        for offset, (message, payload) in enumerate(turns):
            row = Message(
                conversation_id=conversation_id,
                sequence=position + offset,
                role=_LLM_TO_ROLE[message.role],
                content=message.content,
                tool_metadata=payload,
            )
            self.session.add(row)
            staged.append(row)

        return staged


def to_llm_messages(rows: Sequence[Message]) -> list[LLMMessage]:
    """Rebuild a conversation the runtime can send to a model.

    A tool turn is reconstructed from its stored payload rather than from its
    text, so the message that goes back to the provider is byte-for-byte the one
    that went the first time. Parsing the rendered form back out would work
    until a tool returned business data containing a closing marker - which is
    exactly the kind of thing a hostile record would contain.

    A system turn is skipped. The agent's instructions are server-side
    configuration and are rebuilt on every step from the agent definition; a
    stored system message must never be able to become one.
    """
    rebuilt: list[LLMMessage] = []

    for row in rows:
        role = _ROLE_TO_LLM.get(row.role)
        if role is None or role is LLMRole.SYSTEM:
            continue

        if role is LLMRole.TOOL:
            payload = row.tool_metadata or {}
            if payload.get("type") != TURN_TOOL_RESULT:
                # A tool turn without its structured form cannot be replayed
                # faithfully. Dropping it is safer than sending the model half a
                # result, and the row itself is untouched.
                continue
            rebuilt.append(LLMMessage.tool_result(tool_result_from_payload(payload)))
            continue

        rebuilt.append(LLMMessage(role=role, content=row.content))

    return rebuilt


def pending_tool_request(rows: Sequence[Message], *, tool_execution_id: uuid.UUID) -> dict | None:
    """The stored tool request an approval is about.

    Found by execution id rather than by position, so a conversation that has
    grown since - or one where two tools were requested - resolves to the right
    turn. Returns the structured payload, or None when there is no such request.
    """
    wanted = str(tool_execution_id)

    for row in reversed(rows):
        payload = row.tool_metadata or {}
        if payload.get("type") != TURN_TOOL_REQUEST:
            continue
        if payload.get("tool_execution_id") == wanted:
            return dict(payload)

    return None
