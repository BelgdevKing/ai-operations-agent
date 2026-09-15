"""The loop that executes one agent run.

    while budget remains:
        ask the model
        -> final answer?      complete the run
        -> tool request?      execute it, append what was asked and what
                              happened, and ask again
        -> needs approval?    record the request, pause, and return

Deliberately small, deliberately iterative, and deliberately auditable. It owns
four things: how the prompt is assembled, what the model is allowed to return,
what gets appended to the conversation, and when to stop. Choosing a provider,
retrying and translating vendor errors all happen below it in the gateway;
resolving, validating and running a tool all happen beside it in the executor;
writing any of it down happens through the journal, which is a protocol here and
a database somewhere else. Nothing in this file knows how any of that is done.

Three invariants are worth naming, because the loop exists to hold them:

* **The model cannot manufacture a result.** A tool turn is built from the
  executor's own `ToolResult`, in `_performed`. There is no path from model
  output to that function.
* **The loop always ends.** Every pass is one model call and the counter is the
  agent's configured budget - counted across the whole run, not per request - so
  a model that keeps asking for tools runs out rather than running forever.
* **A pause is not an ending.** A tool that needs a person leaves the run in
  `awaiting_approval` with its request recorded, and `resume` picks up the same
  run rather than starting a second one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError as PydanticValidationError

from app.agents.cancellation import NEVER_CANCELLED, CancellationToken
from app.agents.decisions import (
    AgentDecisionEnvelope,
    FinalDecision,
    ToolRequestDecision,
)
from app.agents.exceptions import (
    AgentCancelledError,
    AgentConversationTooLargeError,
    AgentInvalidDecisionError,
    AgentMaxStepsExceededError,
)
from app.agents.journal import (
    NULL_JOURNAL,
    RecordedTurn,
    RunJournal,
    ToolAttempt,
    tool_request_payload,
    tool_result_payload,
)
from app.agents.models import AgentContext, AgentRun, AgentStep
from app.ai.gateway import LLMGateway
from app.ai.models import LLMMessage, LLMRequest, LLMToolResult
from app.tools.executor import ToolExecutor
from app.tools.models import ToolOutcome, ToolRequest, ToolResult

logger = logging.getLogger(__name__)


class AgentRunner:
    """Executes one run against one gateway."""

    def __init__(
        self,
        gateway: LLMGateway,
        *,
        default_model: str,
        tools: ToolExecutor,
        max_messages: int,
        max_characters: int,
    ) -> None:
        self._gateway = gateway
        self._default_model = default_model
        self._tools = tools
        # The same limits the runtime applies to what a caller sends, applied
        # again as the conversation grows. A tool result is the one part nobody
        # here chose the size of.
        self._max_messages = max_messages
        self._max_characters = max_characters

    async def run(
        self,
        context: AgentContext,
        run: AgentRun,
        *,
        journal: RunJournal = NULL_JOURNAL,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> AgentRun:
        """Drive *run* forward until it ends or pauses.

        The run is mutated as it goes and returned. Failures are recorded on it
        *and* raised: the API layer needs the exception to choose a status code,
        and anything observing the run needs the record.

        Raises:
            AgentCancelledError: Stopped between steps, or around a tool.
            AgentMaxStepsExceededError: The step budget ran out first.
            AgentConversationTooLargeError: Tool results outgrew the limits.
            AgentInvalidDecisionError: Output matched the schema but was unusable.
            LLMError: Any provider failure, already normalised by the gateway
                and never rewritten here.
        """
        run.start()
        await journal.record_state(run)

        # The conversation this run is building. It starts as what the caller
        # sent and grows by two turns per tool: what the agent asked for, and
        # what the framework actually did. It is local to the run - the caller's
        # own message list is never mutated.
        conversation: list[LLMMessage] = list(context.messages)

        return await self._drive(context, run, conversation, journal, cancellation)

    async def resume(
        self,
        context: AgentContext,
        run: AgentRun,
        *,
        request: ToolRequestDecision,
        result: ToolResult,
        journal: RunJournal = NULL_JOURNAL,
        cancellation: CancellationToken = NEVER_CANCELLED,
    ) -> AgentRun:
        """Carry on a paused run, now that its tool has an outcome.

        The **same run**: it keeps its id, its conversation, its step budget and
        everything it had already spent. There is no second run, and nothing
        about the audit trail becomes harder to follow because a person took an
        hour to answer.

        *result* is whatever the framework did once a decision existed - the
        tool's own output if it was approved, or a ``REJECTED`` result if it was
        not. Either way it enters the conversation through `_performed`, like
        every other tool result, and the bounded loop decides what to do next.
        A refusal is not turned into a failure and not turned into a success.

        *context.messages* is expected to be the conversation as it stood when
        the run paused - including the assistant turn that asked for the tool.
        """
        run.resume()
        await journal.record_state(run)

        conversation: list[LLMMessage] = list(context.messages)
        performed = self._performed(request, result)
        conversation.append(performed)

        try:
            await self._record_performed(run, performed, journal)
            self._enforce_conversation_limits(conversation)
        except Exception as exc:
            self._record_failure(run, exc)
            await journal.record_state(run)
            raise

        return await self._drive(context, run, conversation, journal, cancellation)

    # -- The loop --------------------------------------------------------------

    async def _drive(
        self,
        context: AgentContext,
        run: AgentRun,
        conversation: list[LLMMessage],
        journal: RunJournal,
        cancellation: CancellationToken,
    ) -> AgentRun:
        agent = context.agent

        try:
            # Iterative, and bounded by the agent's own budget. Not recursion:
            # a loop with an explicit counter is auditable at a glance, and the
            # counter is the only thing standing between a model that keeps
            # asking for tools and a request that never ends.
            #
            # Every pass is one model call, so max_steps bounds spend as well as
            # time. A request cannot widen it; max_steps of zero means the body
            # never runs and the budget error below is raised immediately. The
            # count is the run's, so resuming after an approval does not hand
            # the agent a fresh budget.
            while run.step_count < agent.max_steps:
                await self._raise_if_cancelled(cancellation, run, journal)

                step = await self._step(context, conversation, number=run.step_count + 1)
                run.record(step)
                await journal.record_step(run, step)

                decision = step.decision

                if isinstance(decision, FinalDecision):
                    answer = LLMMessage.assistant(decision.content)
                    run.complete(final_response=decision.content)
                    await journal.record_turns(run, [RecordedTurn(message=answer)])
                    await journal.record_state(run)
                    self._log_outcome(run, outcome="final")
                    return run

                if isinstance(decision, ToolRequestDecision):
                    await self._raise_if_cancelled(cancellation, run, journal)

                    result = await self._execute_tool(context, run, decision, cancellation)
                    run.record_tool(decision, result)

                    attempt = ToolAttempt(
                        step_number=run.step_count,
                        tool_name=decision.tool_name,
                        argument_count=len(decision.arguments),
                        executed=reached_the_tool(result),
                        result=result,
                    )
                    await journal.record_tool(run, attempt)

                    # The assistant turn goes in either way: it is what the
                    # agent asked for, and a paused run needs it to know what to
                    # replay when somebody says yes.
                    requested = self._requested(decision)
                    conversation.append(requested)
                    await journal.record_turns(
                        run,
                        [
                            RecordedTurn(
                                message=requested,
                                payload=tool_request_payload(
                                    decision, tool_execution_id=result.tool_execution_id
                                ),
                            )
                        ],
                    )

                    if result.outcome is ToolOutcome.APPROVAL_REQUIRED:
                        # Somebody has to decide. Not an answer, not a failure,
                        # and emphatically not a completed run.
                        await journal.record_approval_request(run, attempt)
                        run.await_approval(tool_execution_id=result.tool_execution_id)
                        await journal.record_state(run)
                        self._log_outcome(run, outcome="awaiting_approval")
                        return run

                    if result.outcome is ToolOutcome.CANCELLED:
                        # Somebody stopped this. Not a result to reason about.
                        await self._raise_if_cancelled(cancellation, run, journal)

                    # The tool turn says what the framework actually did.
                    # Keeping it distinct from the request is what stops a
                    # model's proposal from reading like an outcome.
                    performed = self._performed(decision, result)
                    conversation.append(performed)
                    await self._record_performed(run, performed, journal)

                    self._enforce_conversation_limits(conversation)
                    continue

                # Unreachable while the union has two members; a third added
                # without updating this loop should fail loudly, not silently
                # keep looping.
                raise AgentInvalidDecisionError()

            # The budget ran out with no answer. Reported rather than truncated:
            # a run that quietly stopped looking would read as a considered
            # response, which it is not.
            raise AgentMaxStepsExceededError()

        except AgentCancelledError:
            raise
        except Exception as exc:
            self._record_failure(run, exc)
            await journal.record_state(run)
            raise

    # -- Tools -----------------------------------------------------------------

    async def _execute_tool(
        self,
        context: AgentContext,
        run: AgentRun,
        decision: ToolRequestDecision,
        cancellation: CancellationToken,
    ) -> ToolResult:
        """Hand the request to the tool framework and take back its result.

        Identity is passed from the run, never from the decision. The decision
        is a model's output and carries only a name and arguments; everything
        the tool is allowed to know about *who* it is acting for comes from
        here, which is what stops a tool being pointed at another tenant.

        No approval is passed. The loop never grants one - a tool that needs a
        person comes back refused, and the only thing that can hand the executor
        a decision is the approval service, acting on a durable row.

        The result is returned whatever it says. A tool that does not exist, is
        disabled, was called wrongly or needs approval all come back as a
        structured result rather than an exception - the run records what was
        asked and what came of it either way.
        """
        return await self._tools.execute(
            ToolRequest(tool_name=decision.tool_name, arguments=decision.arguments),
            organization_id=context.organization_id,
            user_id=context.user_id,
            run_id=run.id,
            agent_id=context.agent.id,
            request_id=context.request_id,
            cancellation=cancellation,
        )

    # -- One model call --------------------------------------------------------

    async def _step(
        self, context: AgentContext, conversation: Sequence[LLMMessage], *, number: int
    ) -> AgentStep:
        started_at = datetime.now(UTC)

        messages = self._build_messages(context, conversation)
        request = self._build_request(context, messages)

        structured = await self._gateway.generate_structured(request, AgentDecisionEnvelope)

        try:
            return AgentStep(
                number=number,
                decision=structured.data.decision,
                # The model, not the provider: which vendor served the call is
                # internal routing, and the response schema deliberately omits it.
                model=structured.response.model,
                usage=structured.response.usage,
                latency_ms=structured.response.latency_ms,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                message_count=len(messages),
            )
        except PydanticValidationError as exc:
            # The adapter validates against the schema, so reaching here means
            # something produced a decision the runtime has no shape for. Raised
            # as an agent error rather than escaping as a Pydantic one, which
            # would leave the API layer with an unhandled 500 and a traceback
            # where a clean refusal belongs.
            raise AgentInvalidDecisionError() from exc

    def _build_messages(
        self, context: AgentContext, conversation: Sequence[LLMMessage]
    ) -> list[LLMMessage]:
        """The agent's instructions, then the conversation so far.

        The system message is built here from the agent definition and nowhere
        else, on every pass. Nothing a caller sent can reach that position - the
        request schema accepts user and assistant turns only - and nothing a
        *tool* returned can either: a tool turn is appended after the prompt,
        never merged into it, however the business data reads. A conversation
        rebuilt from the database gets the same treatment: the instructions come
        from the agent definition, never from a stored message.
        """
        return [LLMMessage.system(context.agent.instructions), *conversation]

    # -- Conversation growth ---------------------------------------------------

    def _requested(self, decision: ToolRequestDecision) -> LLMMessage:
        """The assistant turn recording what the agent asked for.

        Written by the runtime from the decision it actually received, not
        echoed from anything the model wrote as prose - so the transcript says
        what was requested rather than what the model claimed to request.
        """
        arguments = json.dumps(decision.arguments, ensure_ascii=False, sort_keys=True, default=str)
        header = f"[tool request: {decision.tool_name}]"
        return LLMMessage.assistant("\n".join([header, arguments, "[end tool request]"]))

    def _performed(self, decision: ToolRequestDecision, result: ToolResult) -> LLMMessage:
        """The tool turn recording what the framework did.

        The only place a tool result enters a conversation, and it is built from
        the executor's `ToolResult` - never from anything the model produced.
        That is the whole guarantee behind "the model cannot fabricate a tool
        result": there is no path from model output to this function. A human
        rejection arrives here as an ordinary outcome, for the same reason.
        """
        return LLMMessage.tool_result(
            LLMToolResult(
                tool_name=decision.tool_name,
                execution_id=str(result.tool_execution_id),
                succeeded=result.ok,
                outcome=result.outcome.value,
                data=result.data,
                error=result.failure.message if result.failure else None,
            )
        )

    def _enforce_conversation_limits(self, conversation: Sequence[LLMMessage]) -> None:
        """Stop a growing transcript from outgrowing what may be sent.

        Checked after every tool, because a tool result is the one part of the
        conversation whose size nobody here chose. A business system can return
        far more than anyone expected, and an unbounded prompt is both a cost
        problem and a way to push the agent's instructions out of attention.
        """
        if len(conversation) > self._max_messages:
            raise AgentConversationTooLargeError()

        total = sum(len(message.transport_content) for message in conversation)
        if total > self._max_characters:
            raise AgentConversationTooLargeError()

    def _build_request(self, context: AgentContext, messages: list[LLMMessage]) -> LLMRequest:
        agent = context.agent

        overrides: dict[str, object] = {}
        # Omitted rather than defaulted, so the internal default applies and an
        # adapter can still tell "not asked for" from "asked for this" - which
        # is how temperature avoids being sent to a model that rejects one.
        if agent.temperature is not None:
            overrides["temperature"] = agent.temperature

        return LLMRequest(
            messages=messages,
            model=agent.model or self._default_model,
            max_output_tokens=agent.max_output_tokens,
            **overrides,
        )

    # -- Bookkeeping -----------------------------------------------------------

    async def _record_performed(
        self, run: AgentRun, performed: LLMMessage, journal: RunJournal
    ) -> None:
        """Store the tool turn as conversation content.

        The structured payload travels with it so a later request can rebuild
        the turn exactly, rather than parsing the rendered text back out - which
        would break the first time a tool returned a string containing a closing
        marker.
        """
        if performed.tool is None:  # pragma: no cover - _performed always sets it
            return

        await journal.record_turns(
            run,
            [RecordedTurn(message=performed, payload=tool_result_payload(performed.tool))],
        )

    async def _raise_if_cancelled(
        self, cancellation: CancellationToken, run: AgentRun, journal: RunJournal
    ) -> None:
        if not cancellation.cancelled:
            return

        run.cancel()
        # Written before raising: a cancelled run that was never recorded as
        # cancelled would be indistinguishable from an abandoned one, and would
        # be swept as a failure later.
        await journal.record_state(run)

        logger.info(
            "Agent run cancelled",
            extra={"context": {"run_id": str(run.id), "steps": run.step_count}},
        )
        raise AgentCancelledError()

    def _record_failure(self, run: AgentRun, exc: Exception) -> None:
        """Mark the run failed, with a message that is safe to hand a client.

        The client-facing text is the exception *class's* message rather than
        the instance's, for the same reason the HTTP layer does it: an instance
        message may name a model, a provider or a piece of deployment
        configuration. The specific one goes to the log.
        """
        if run.is_terminal:
            return

        code = getattr(exc, "code", "agent_run_error")
        safe_message = getattr(type(exc), "message", None) or "The agent run failed."

        run.fail(code=code, message=safe_message)

        logger.warning(
            "Agent run failed",
            extra={
                "context": {
                    "run_id": str(run.id),
                    "agent_id": str(run.agent_id),
                    "organization_id": str(run.organization_id),
                    "steps": run.step_count,
                    "error": type(exc).__name__,
                    "code": code,
                }
            },
        )

    def _log_outcome(self, run: AgentRun, *, outcome: str) -> None:
        """Operational metadata only.

        No prompt, no completion, no tool arguments: a run's text is the user's
        business data, and a log is the wrong place for it.
        """
        logger.info(
            "Agent run finished",
            extra={
                "context": {
                    "run_id": str(run.id),
                    "agent_id": str(run.agent_id),
                    "organization_id": str(run.organization_id),
                    "request_id": run.request_id,
                    "outcome": outcome,
                    "steps": run.step_count,
                    "total_tokens": run.usage.total_tokens,
                    "latency_ms": round(run.latency_ms, 1),
                }
            },
        )


def reached_the_tool(result: ToolResult) -> bool:
    """Whether the tool's body actually ran.

    Everything the framework refuses before calling it - a missing tool, a
    disabled one, bad arguments, a pending approval, a rejection, a cancellation
    noticed in time - leaves the world untouched, and the durable record must
    say so. Only a result the tool itself produced counts, plus a timeout, which
    is the honest case: the deadline expired inside the tool and the side effect
    may well have happened.
    """
    return result.outcome in {ToolOutcome.SUCCEEDED, ToolOutcome.FAILED, ToolOutcome.TIMED_OUT}
