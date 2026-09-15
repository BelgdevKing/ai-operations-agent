"""Reading stored conversations.

Read-only. A conversation is written by a run, never by a client: there is no
endpoint here that creates, edits or deletes a turn, because the whole value of
a stored conversation is that it says what actually happened.

**This is the content store, so the tenant scope is the security boundary.** A
conversation holds the user's questions, the agent's answers and the business
data its tools returned. Every read below goes through
``ConversationRepository``, which is tenant-scoped, and ``messages`` is only ever
reached by joining back to a conversation this organization owns - the table has
no tenant column of its own by design.

What a client is given is deliberately less than what is stored: a tool request
becomes a name, and a tool result becomes a name and an outcome. The records the
tools read reach the reader through the agent's answer, which is the turn that
was written for them.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentOrganizationId, RequireMember, SessionDep
from app.repositories.conversation import ConversationRepository
from app.schemas.agent import ConversationDetail, ConversationSummary, ConversationTurn
from app.schemas.common import ErrorResponse
from app.services.agent_execution import ConversationNotFoundError

router = APIRouter()

RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "Not a member of an active organization"},
    404: {"model": ErrorResponse, "description": "No such conversation for this organization"},
}


@router.get(
    "/conversations",
    response_model=list[ConversationSummary],
    summary="Stored conversations",
    responses={401: RESPONSES[401], 403: RESPONSES[403]},
)
async def list_conversations(
    membership: RequireMember,
    organization_id: CurrentOrganizationId,
    session: SessionDep,
    limit: int = 20,
) -> list[ConversationSummary]:
    """This organization's conversations, newest first."""
    del membership
    repository = ConversationRepository(session, organization_id)
    conversations = await repository.list_recent(limit=max(1, min(limit, 100)))
    return [ConversationSummary.from_record(record) for record in conversations]


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="One stored conversation and its turns",
    responses=RESPONSES,
)
async def get_conversation(
    conversation_id: uuid.UUID,
    membership: RequireMember,
    organization_id: CurrentOrganizationId,
    session: SessionDep,
) -> ConversationDetail:
    """Read one of this organization's conversations.

    A conversation belonging to another tenant answers 404, exactly as one that
    does not exist does - so an id cannot be used to discover whether another
    organization has been asking about something.
    """
    del membership

    repository = ConversationRepository(session, organization_id)

    conversation = await repository.get_active(conversation_id)
    if conversation is None:
        raise ConversationNotFoundError()

    rows = await repository.messages(conversation.id)

    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        turns=[ConversationTurn.from_record(row) for row in rows],
    )
