"""The application's way in to the agent runtime.

    execution service  ->  AgentRuntime  ->  AgentRunner  ->  LLMGateway

It resolves which agent is being asked for, decides whether this tenant may run
it, builds the context, and hands it to the runner with whatever journal it was
given. It does not call a provider, does not interpret a decision, and does not
touch a database - the journal is a protocol, and what is behind it is somebody
else's business.

It is the only place that turns "an agent id and some messages" into "a run
belonging to an organization", which is why the tenant arrives here already
established and is never read from anything a caller sent.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from app.agents.cancellation import NEVER_CANCELLED, CancellationToken
from app.agents.decisions import ToolRequestDecision
from app.agents.exceptions import AgentConfigurationError
from app.agents.journal import NULL_JOURNAL, RunJournal
from app.agents.models import Agent, AgentContext, AgentRun
from app.agents.registry import AgentRegistry
from app.agents.runner import AgentRunner
from app.ai.gateway import LLMGateway
from app.ai.models import LLMMessage
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.tools.executor import ToolExecutor
from app.tools.models import ToolResult
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Runs agents on behalf of a verified caller."""

    def __init__(
        self,
        gateway: LLMGateway,
        registry: AgentRegistry,
        settings: Settings,
        tools: ToolExecutor | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings

        # An empty registry when none is supplied, rather than no tool support
        # at all. A tool request then comes back as a structured "that tool is
        # not available" result instead of an exception nobody expected - which
        # is also exactly what a deployment with no tools installed should say.
        executor = tools or ToolExecutor(ToolRegistry(), settings)

        self._runner = AgentRunner(
            gateway,
            default_model=settings.llm_model,
            tools=executor,
            # The same limits that bound the incoming request, applied again as
            # tool results grow the conversation.
            max_messages=settings.agent_max_messages,
            max_characters=settings.agent_max_conversation_characters,
        )

    def available_to(self, organization_id: uuid.UUID) -> list[Agent]:
        """Every agent this organization may run.

        Scoped at the source rather than filtered afterwards, so another
        tenant's agent is never a candidate in the first place.
        """
        return self._registry.list_for(organization_id)

    def resolve(self, agent_id: uuid.UUID, organization_id: uuid.UUID) -> Agent:
        """The agent, if this organization may run it.

        Exposed so the layer that creates a durable run can refuse an unknown or
        another tenant's agent *before* writing a row for it.

        Raises:
            AgentNotFoundError: No such agent, or it belongs to another tenant.
            AgentDisabledError: The agent is switched off.
        """
        return self._registry.resolve(agent_id, organization_id)

    async def run(
        self,
        agent_id: uuid.UUID,
        messages: Sequence[LLMMessage],
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        run: AgentRun | None = None,
        journal: RunJournal = NULL_JOURNAL,
        request_id: str | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> AgentRun:
        """Execute one run.

        Args:
            agent_id: Which agent. Checked against *organization_id*.
            messages: The conversation, oldest first. User and assistant turns
                only; the system prompt comes from the agent definition.
            organization_id: The tenant this run belongs to. Comes from the
                caller's verified membership - never from a request body, and
                never from anything the browser can set.
            user_id: Who asked, for attribution.
            run: The run to advance. Supplied by the layer that created the
                durable record, so the id in the database and the id in the
                runtime are one value. A fresh one is made when it is omitted,
                which is what the tests that predate persistence do.
            journal: Where to write the run down as it goes.
            request_id: HTTP correlation id, carried onto the run.
            cancellation: Checked between steps.

        Raises:
            AgentNotFoundError: No such agent, or it belongs to another tenant.
            AgentDisabledError: The agent is switched off.
            AgentConfigurationError: The deployment has no model configured.
            ValidationError: The conversation is empty or too large.
            AgentError: Anything the run itself did wrong.
            LLMError: Any provider failure, already normalised.
        """
        # Resolution first, and scoped to the tenant: an agent belonging to
        # another organization is missing, not forbidden.
        agent = self._registry.resolve(agent_id, organization_id)

        self._validate_conversation(messages)
        self._require_a_model(agent)

        if run is None:
            run = AgentRun(
                agent_id=agent.id,
                organization_id=organization_id,
                user_id=user_id,
                request_id=request_id,
            )

        context = self._context(agent, run, messages, request_id=request_id)

        logger.info(
            "Agent run starting",
            extra={
                "context": {
                    "run_id": str(run.id),
                    "agent_id": str(agent.id),
                    "organization_id": str(organization_id),
                    "messages": len(messages),
                    "max_steps": agent.max_steps,
                }
            },
        )

        return await self._runner.run(context, run, journal=journal, cancellation=cancellation)

    async def resume(
        self,
        run: AgentRun,
        messages: Sequence[LLMMessage],
        *,
        request: ToolRequestDecision,
        result: ToolResult,
        journal: RunJournal = NULL_JOURNAL,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> AgentRun:
        """Continue a paused run once its tool has an outcome.

        The agent is resolved again from the run's own organization, so a run
        cannot be resumed into a tenant it does not belong to, and an agent that
        has since been disabled or removed stops the resume rather than being
        silently re-created from the stored conversation.

        The conversation limits are *not* re-applied to *messages* here: it is
        the run's own transcript rebuilt from storage, not new input, and
        refusing to resume a run because the transcript it already has is large
        would strand it in ``awaiting_approval`` forever. The runner still
        enforces the limits as the conversation grows from this point.
        """
        agent = self._registry.resolve(run.agent_id, run.organization_id)
        self._require_a_model(agent)

        context = self._context(agent, run, messages, request_id=run.request_id)

        logger.info(
            "Agent run resuming",
            extra={
                "context": {
                    "run_id": str(run.id),
                    "agent_id": str(agent.id),
                    "organization_id": str(run.organization_id),
                    "messages": len(messages),
                    "steps_so_far": run.step_count,
                    "outcome": result.outcome.value,
                }
            },
        )

        return await self._runner.resume(
            context,
            run,
            request=request,
            result=result,
            journal=journal,
            cancellation=cancellation,
        )

    # -- Building a context ----------------------------------------------------

    def _context(
        self,
        agent: Agent,
        run: AgentRun,
        messages: Sequence[LLMMessage],
        *,
        request_id: str | None,
    ) -> AgentContext:
        return AgentContext(
            run_id=run.id,
            organization_id=run.organization_id,
            user_id=run.user_id,
            agent=agent,
            messages=tuple(messages),
            request_id=request_id,
        )

    # -- Limits ----------------------------------------------------------------

    def _require_a_model(self, agent: Agent) -> None:
        if not (agent.model or self._settings.llm_model):
            raise AgentConfigurationError()

    def _validate_conversation(self, messages: Sequence[LLMMessage]) -> None:
        """Bound the conversation from server-side configuration.

        The API schema bounds it too. This is the runtime's own guard, so a
        caller reaching the runtime by some other route - a future scheduled
        run, an internal service - cannot exceed it either.
        """
        if not messages:
            raise ValidationError("At least one message is required.")

        if len(messages) > self._settings.agent_max_messages:
            raise ValidationError(
                f"A run accepts at most {self._settings.agent_max_messages} messages."
            )

        total = sum(len(message.content) for message in messages)
        if total > self._settings.agent_max_conversation_characters:
            raise ValidationError("The conversation is too large for one run. Start a new one.")
