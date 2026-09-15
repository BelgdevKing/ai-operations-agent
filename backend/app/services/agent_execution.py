"""Starting a durable agent run, and reading one back.

    endpoint -> AgentExecutionService -> AgentRuntime -> AgentRunner -> gateway
                       |                      |
                       v                      v
                  PostgreSQL            RunJournal -> PostgreSQL

The service owns everything the runtime deliberately does not: which tenant,
which conversation, whether this work has already been done, and what to write
down. The runtime owns the loop. Keeping the two apart is what lets the loop be
tested with no database and the persistence be tested with no model.

Four things happen here that are worth reading before changing anything.

**The durable run is the authoritative record.** Every response is projected
from the ``agent_runs`` row and the stored conversation, never from whatever the
in-memory run happened to hold - so a replayed request and a fresh one answer
the same way, and a run that is read back after a process restart reads the same
as it did before.

**Idempotency is the database's.** ``UNIQUE(organization_id, idempotency_key)``
decides who wins a race; the loser reads the winner's row. Nothing is cached in
this process, because two processes would not share a cache.

**Transactions are short.** The run is created and committed, the conversation
is created and committed, the new turns are committed, and only then does the
first model call happen. No transaction is open across a call to a provider.

**A run may pause.** A tool that needs a person leaves the run in
``awaiting_approval`` with its request recorded and an approval row waiting.
Nothing here decides an approval; that is :mod:`app.services.approvals`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cancellation import NEVER_CANCELLED, CancellationToken
from app.agents.exceptions import AgentCancelledError, AgentError
from app.agents.models import AgentRun, AgentRunCarryover, AgentRunStatus
from app.agents.runtime import AgentRuntime
from app.ai.exceptions import LLMError
from app.ai.models import LLMMessage, LLMRole
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.agent_run import AgentRunRecord, ToolExecutionRecord
from app.models.approval import Approval
from app.models.enums import ApprovalStatus, MessageRole
from app.models.organization import OrganizationMember
from app.observability.instruments import Instruments, NullInstruments
from app.observability.tracing import Span, span
from app.repositories.agent_run import AgentRunRepository, ToolExecutionRepository
from app.repositories.approval import ApprovalRepository
from app.repositories.conversation import ConversationRepository
from app.services.audit import (
    record_agent_run,
    record_agent_run_created,
    record_approval_cancelled,
    record_approval_requested,
)
from app.services.run_journal import DatabaseRunJournal
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ConversationNotFoundError(NotFoundError):
    """No such conversation for this organization.

    One answer for "does not exist", "belongs to another tenant" and "has been
    archived", for the reason every scoped lookup uses: a different answer would
    confirm another organization's conversation exists to anyone who could guess
    an id.
    """

    code = "conversation_not_found"
    message = "That conversation does not exist."


class RunNotFoundError(NotFoundError):
    """No such run for this organization."""

    code = "agent_run_not_found"
    message = "That agent run does not exist."


class RunNotCancellableError(ConflictError):
    """The run is not in a state a person can stop.

    Cancellation ends a run that is *waiting*, and nothing else. A run that has
    finished has nothing to stop, and a run that is mid-flight is being driven
    by another request - ending it from here would leave that request writing
    into a record the database says is closed.
    """

    code = "agent_run_not_cancellable"
    message = "That run is not waiting for approval, so it cannot be cancelled."


@dataclass(frozen=True)
class RunView:
    """One run as a client is allowed to see it, read from the durable record.

    Assembled rather than returned raw so that adding a column to ``agent_runs``
    cannot accidentally publish it.
    """

    record: AgentRunRecord
    final_response: str | None
    tool_executions: Sequence[ToolExecutionRecord]
    pending_approval: Approval | None
    replayed: bool = False


class AgentExecutionService:
    """Runs agents durably, for one verified membership."""

    def __init__(
        self,
        session: AsyncSession,
        runtime: Callable[[], AgentRuntime],
        settings: Settings,
        membership: OrganizationMember,
        tools: ToolRegistry | None = None,
        instruments: Instruments | None = None,
    ) -> None:
        self._session = session
        # A factory rather than a runtime, for the same reason the approval
        # service takes one: building an ``AgentRuntime`` builds the LLM gateway,
        # which fails on a deployment with no provider credential. Reading a run
        # back and cancelling a paused one never touch a model, and answering
        # either with a 500 about a provider would be a lie about what went
        # wrong. Only the paths that actually call a model call this.
        self._runtime = runtime
        self._settings = settings
        self._tools = tools
        # Recorded at the same chokepoint the audit trail uses, so the two
        # cannot end up counting different things.
        self._instruments = instruments or NullInstruments()

        # Fixed from the caller's verified membership, once, here. Nothing below
        # takes an organization from an argument, so there is no expression a
        # caller could write that would move a run into another tenant.
        self._organization_id = membership.organization_id
        self._user_id = membership.user_id

        self._runs = AgentRunRepository(session, self._organization_id)
        self._executions = ToolExecutionRepository(session, self._organization_id)
        self._approvals = ApprovalRepository(session, self._organization_id)
        self._conversations = ConversationRepository(session, self._organization_id)

    # -- Starting a run --------------------------------------------------------

    async def start(
        self,
        agent_id: uuid.UUID,
        messages: Sequence[LLMMessage],
        *,
        conversation_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> RunView:
        """Create a durable run and drive it until it ends or pauses.

        Raises:
            AgentNotFoundError: No such agent, or it belongs to another tenant.
            AgentDisabledError: The agent is switched off.
            ConversationNotFoundError: The named conversation is not this
                organization's, or is no longer active.
            ValidationError: The request does not fit the conversation rules.
            AgentError / LLMError: Whatever the run itself ran into.
        """
        # An agent that does not exist for this tenant must not leave a row
        # behind, so resolution comes before anything is written.
        agent = self._runtime().resolve(agent_id, self._organization_id)

        await self._sweep_abandoned()

        run_id = uuid.uuid4()
        record, created = await self._runs.create_or_claim(
            run_id=run_id,
            user_id=self._user_id,
            agent_id=agent.id,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

        if not created:
            # Somebody already did this work, or is doing it now. The honest
            # answer is what that run is, not a second run doing it again.
            logger.info(
                "Agent run replayed from idempotency key",
                extra={
                    "context": {
                        "run_id": str(record.id),
                        "organization_id": str(self._organization_id),
                        "status": record.status.value,
                    }
                },
            )
            await record_agent_run_created(self._session, record, idempotent_replay=True)
            return await self._view(record, replayed=True)

        await record_agent_run_created(self._session, record)

        history, conversation_id = await self._prepare_conversation(
            conversation_id, messages, record
        )

        journal = DatabaseRunJournal(
            self._session,
            organization_id=self._organization_id,
            record=record,
            conversation_id=conversation_id,
            registry=self._tools,
            approval_expires_after=timedelta(seconds=self._settings.approval_expiration_seconds),
        )

        run = AgentRun(
            id=record.id,
            agent_id=agent.id,
            organization_id=self._organization_id,
            user_id=self._user_id,
            request_id=request_id,
        )

        await self._drive(
            lambda: self._runtime().run(
                agent_id,
                history,
                organization_id=self._organization_id,
                user_id=self._user_id,
                run=run,
                journal=journal,
                request_id=request_id,
                cancellation=cancellation,
            ),
            run,
        )

        return await self._view(record)

    async def run_for_workflow(
        self,
        *,
        agent_id: str,
        message: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> RunView:
        """Run an agent as one step of a workflow.

        A thin adapter, and deliberately thin: the workflow engine asks for an
        agent run and gets exactly what the AI workspace gets, through the same
        method, with the same durability, the same conversation and the same
        audit trail. There is no workflow-specific agent path to diverge.

        The idempotency key is the workflow step's own id, so a step somehow
        attempted twice reaches the run it already made rather than paying for a
        second one. Each step gets its own conversation: a workflow's steps are
        separate questions, and threading them into one transcript would let an
        earlier step's business data reach a later step's prompt without the
        definition saying so.

        Raises:
            AgentNotFoundError: No such agent, or it belongs to another tenant -
                which is also what a workflow naming somebody else's agent gets,
                though activation refuses that long before here.
            WorkflowInputError: The agent id in the definition is not a uuid.
        """
        try:
            resolved = uuid.UUID(agent_id)
        except ValueError as exc:
            raise ValidationError("That workflow names an agent id that is not valid.") from exc

        return await self.start(
            resolved,
            [LLMMessage.user(message)],
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    # -- Reading runs back -----------------------------------------------------

    async def get(self, run_id: uuid.UUID) -> RunView:
        """One of this organization's runs."""
        record = await self._runs.get(run_id)
        if record is None:
            raise RunNotFoundError()
        return await self._view(record)

    async def list_recent(self, *, limit: int = 20) -> list[RunView]:
        """This organization's runs, newest first."""
        return [await self._view(record) for record in await self._runs.list_recent(limit=limit)]

    async def cancel(self, run_id: uuid.UUID) -> RunView:
        """Stop a run that is waiting on somebody, and withdraw its question.

        Order matters, and it is the approval first. Both statements are
        conditional and both have ``pending`` in the ``WHERE``, so the approval
        row decides which of two simultaneous requests wins:

        * this call withdraws the approval - a decision arriving after that
          changes no row and is told the approval is already decided, having
          executed nothing;
        * a decision withdraws it first - this call finds nothing to withdraw,
          refuses, and touches the run at all.

        What cannot happen is the action running after the run is durably
        cancelled. It *could* when the run was cancelled first: a decision that
        had already won the approval row went on to claim the execution and
        resume the run over the top of the cancellation, and both requests
        answered 200.

        Raises:
            RunNotFoundError: Not this organization's run.
            RunNotCancellableError: It is not waiting for anybody.
        """
        record = await self._runs.get(run_id)
        if record is None:
            raise RunNotFoundError()

        # **The approval row is the arbiter, and it is taken first.**
        #
        # Cancelling the run first looked right and was not: a decision that had
        # already won the approval row was, at that moment, on its way to
        # claiming the execution and resuming the run - so the cancellation
        # landed, this call answered 200, and the tool then ran anyway and wrote
        # the run back to completed. Both sides won, which is the one outcome
        # this pair of conditional updates exists to prevent.
        #
        # Withdrawing the approval first makes that impossible. Whichever
        # request moves ``approvals.status`` out of ``pending`` has won:
        # a decision that gets there first leaves nothing to withdraw and this
        # call refuses, and a cancellation that gets there first leaves nothing
        # to decide and the decision is told so.
        pending = [
            approval
            for approval in await self._approvals.list_for_run(record.id)
            if approval.status is ApprovalStatus.PENDING
        ]
        withdrawn = await self._approvals.cancel_for_run(record.id)

        if withdrawn == 0:
            # Nothing pending was withdrawn, so this call did not win. Either a
            # decision is already recorded and is resuming this run at this
            # moment, or the run is not actually waiting on anybody. Refusing
            # covers both, and nothing is written: the raise rolls the
            # transaction back before the run itself is touched.
            #
            # Reading the pending list first and checking *that* is not enough,
            # and was the first attempt: a decision that had already been
            # recorded leaves nothing pending to read, so the check passed and
            # the cancellation went ahead into the resume. The update's own
            # rowcount is the only thing that proves this call won.
            raise RunNotCancellableError()

        if not await self._runs.cancel_paused(record.id):
            raise RunNotCancellableError()

        for approval in pending:
            await record_approval_cancelled(self._session, approval, cancelled_by=self._user_id)

        await self._session.commit()

        for _ in range(withdrawn):
            self._instruments.record_approval_decision(decision="cancelled", waited_seconds=None)
        await self._session.refresh(record)

        logger.info(
            "Agent run cancelled",
            extra={
                "context": {
                    "run_id": str(record.id),
                    "organization_id": str(self._organization_id),
                }
            },
        )
        return await self._view(record)

    async def _view(self, record: AgentRunRecord, *, replayed: bool = False) -> RunView:
        """Project a durable run onto what a client may see."""
        executions = await self._executions.list_for_run(record.id)

        final_response = None
        if record.status is AgentRunStatus.COMPLETED and record.conversation_id is not None:
            final_response = await self._final_response(record.conversation_id)

        pending: Approval | None = None
        if record.status is AgentRunStatus.AWAITING_APPROVAL:
            pending = next(
                (
                    approval
                    for approval in await self._approvals.list_for_run(record.id)
                    if approval.status is ApprovalStatus.PENDING
                ),
                None,
            )

        return RunView(
            record=record,
            final_response=final_response,
            tool_executions=executions,
            pending_approval=pending,
            replayed=replayed,
        )

    async def _final_response(self, conversation_id: uuid.UUID) -> str | None:
        """The agent's answer, from the conversation.

        Read from the content store rather than copied into ``agent_runs``: the
        answer is a turn, and turns live in one place. That is also what makes an
        idempotent replay able to return the original answer without the
        operational record holding a copy of it.

        The last assistant turn that is not a tool request - those carry a
        structured payload, and an answer does not.
        """
        rows = await self._conversations.messages(conversation_id)
        for row in reversed(rows):
            if row.role is MessageRole.ASSISTANT and row.tool_metadata is None:
                return row.content
        return None

    # -- Conversation ----------------------------------------------------------

    async def _prepare_conversation(
        self,
        conversation_id: uuid.UUID | None,
        messages: Sequence[LLMMessage],
        record: AgentRunRecord,
    ) -> tuple[list[LLMMessage], uuid.UUID]:
        """Attach the run to a conversation and store the new turns.

        Two shapes, and the difference is who owns the history:

        * **No conversation named.** The client is asserting the whole exchange,
          as the stateless endpoint always allowed. A conversation is created and
          every message is stored.
        * **A conversation named.** The history is the server's, and the request
          may add exactly one user turn to it. A client cannot rewrite, reorder
          or replace what is already stored - which is also why a retry cannot
          duplicate a turn: the only thing it can send is the same single
          message, and the idempotency key stops that reaching here twice.
        """
        from app.repositories.conversation import to_llm_messages

        history: list[LLMMessage] = []
        new_turns = list(messages)

        if conversation_id is None:
            conversation = await self._conversations.create(
                user_id=self._user_id, title=_title_from(messages)
            )
        else:
            existing = await self._conversations.get_active(conversation_id)
            if existing is None:
                raise ConversationNotFoundError()

            self._require_one_new_user_turn(messages)
            conversation = existing
            history = to_llm_messages(await self._conversations.messages(conversation.id))

        record.conversation_id = conversation.id
        await self._conversations.append_all(
            conversation.id, [(message, None) for message in new_turns]
        )

        await self._session.commit()

        return [*history, *new_turns], conversation.id

    def _require_one_new_user_turn(self, messages: Sequence[LLMMessage]) -> None:
        if len(messages) != 1 or messages[0].role is not LLMRole.USER:
            raise ValidationError(
                "Continuing a conversation sends exactly one new user message; "
                "the history is the server's."
            )

    # -- Driving, and recording the outcome ------------------------------------

    async def _drive(self, call: Callable[[], Awaitable[AgentRun]], run: AgentRun) -> None:
        """Run the agent and write the audit trail, whatever happened.

        The exception is re-raised: the API layer needs it to choose a status
        code. The audit row is written first, because a failed run that left no
        trace is the case an audit trail exists for.

        The span wrapping it is what the model calls and the tool calls below
        become children of, so a trace shows where one request's time actually
        went. It carries the run's *state* and two counts - never the run's id,
        the agent's id, the conversation or anything the agent was asked.
        """
        with span("agent.run") as current:
            try:
                await call()
            except (AgentCancelledError, AgentError, LLMError):
                await self._audit_outcome(run)
                self._describe(current, run)
                raise

            await self._audit_outcome(run)
            self._describe(current, run)

    @staticmethod
    def _describe(current: Span, run: AgentRun) -> None:
        """Put the run's outcome on the span, from the same values the metric used."""
        current.set_attributes(
            {
                "agent.status": run.status.value,
                "agent.step_count": run.step_count,
                "agent.tool_call_count": len(run.tool_calls),
            }
        )
        if run.error_code:
            current.set_attributes({"error.code": run.error_code, "error.layer": "agent"})
            current.set_status("error")

    async def _audit_outcome(self, run: AgentRun) -> None:
        await record_agent_run(self._session, run)

        # One measurement per request that advanced this run, which is the same
        # grain as the audit event beside it. A run that pauses for approval and
        # later resumes is therefore counted once as `awaiting_approval` and
        # once as whatever it finally became - not once overall, and not twice
        # as completed.
        self._instruments.record_agent_run(status=run.status.value, milliseconds=run.latency_ms)
        if run.error_code:
            self._instruments.record_error(error_code=run.error_code, layer="agent")

        if run.status is AgentRunStatus.AWAITING_APPROVAL and run.pending_tool_execution_id:
            approval = await self._approvals.get_for_execution(run.pending_tool_execution_id)
            if approval is not None:
                await record_approval_requested(self._session, approval)

    # -- Recovery --------------------------------------------------------------

    async def _sweep_abandoned(self) -> int:
        """Fail this organization's runs that nothing is driving any more.

        Run here rather than on a timer because there is no timer: this
        architecture has no background worker, and a sweep that only happens when
        an operator remembers to run a script is a sweep that does not happen. It
        is a single indexed UPDATE scoped to one tenant, so the cost of doing it
        on the way into a run is not worth optimising away.

        It does not free the idempotency key - the row keeps it - and that is
        the point. A client retrying with the same key is handed a run that says
        ``agent_run_abandoned`` rather than one that reads ``pending`` forever,
        so the retry gets a definite answer about the attempt it is repeating
        and the person can start a fresh one.
        """
        failed = await self._runs.sweep_abandoned(
            stale_after=timedelta(seconds=self._settings.agent_run_stale_after_seconds)
        )
        if failed:
            await self._session.commit()
            logger.warning(
                "Abandoned agent runs failed",
                extra={
                    "context": {
                        "organization_id": str(self._organization_id),
                        "runs": failed,
                        "code": "agent_run_abandoned",
                    }
                },
            )
        return failed


def build_carryover(record: AgentRunRecord) -> AgentRunCarryover:
    """What a resumed run has already spent, from its durable record.

    The step budget bounds the *run*, so a run that used three of its four steps
    before pausing gets one more when it resumes - not four. Reading that from
    the row rather than from memory is the whole reason the row is authoritative.
    """
    return AgentRunCarryover(
        step_count=record.step_count,
        tool_call_count=record.tool_call_count,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        latency_ms=float(record.latency_ms),
    )


def _title_from(messages: Sequence[LLMMessage]) -> str | None:
    """A conversation's title, from its first user turn.

    Truncated to the column. It is the user's own words, which is the only thing
    that makes a conversation list navigable - and the conversation already
    stores them in full, so this discloses nothing new.
    """
    for message in messages:
        if message.role is LLMRole.USER:
            text = message.content.strip().splitlines()[0] if message.content.strip() else ""
            return text[:120] or None
    return None
