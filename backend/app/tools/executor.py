"""The controlled boundary every tool call passes through.

    request (untrusted)  +  identity (trusted)
            -> resolve -> enabled? -> permitted? -> validate arguments
            -> approval? -> cancellation? -> execute under a deadline
            -> validate result -> ToolResult

Everything here is policy that must hold for *every* tool, which is why it
lives once in the framework rather than being remembered by each tool author.
No business logic belongs in this file, and no tool may reach its own body
without having passed each step above.

The method always returns a `ToolResult`. Failures become results rather than
exceptions because the caller is an agent run, and a run needs something to
record whatever happened - including "that tool does not exist".
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ValidationError

from app.agents.cancellation import NEVER_CANCELLED, CancellationToken
from app.core.config import Settings
from app.observability.instruments import Instruments, NullInstruments
from app.tools.base import AnyTool
from app.tools.exceptions import (
    ToolApprovalRejectedError,
    ToolApprovalRequiredError,
    ToolAuthorizationError,
    ToolDisabledError,
    ToolError,
    ToolExecutionError,
    ToolInvalidResultError,
    ToolTimeoutError,
    ToolValidationError,
)
from app.tools.models import (
    RESERVED_ARGUMENT_NAMES,
    ApprovalGrant,
    ToolExecutionContext,
    ToolFailure,
    ToolMetadata,
    ToolOutcome,
    ToolRequest,
    ToolResult,
)
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Outcomes that are not the framework's fault, mapped from the exception that
# expresses them. Anything not listed is a failure.
_OUTCOME_BY_ERROR: dict[type[ToolError], ToolOutcome] = {
    ToolTimeoutError: ToolOutcome.TIMED_OUT,
    ToolApprovalRequiredError: ToolOutcome.APPROVAL_REQUIRED,
    ToolApprovalRejectedError: ToolOutcome.REJECTED,
}


class ToolPolicy(Protocol):
    """Decides whether a tool may be reached in a given context.

    A seam rather than an engine. Today every enabled tool is permitted; when
    per-organization enablement arrives it replaces this one object instead of
    being threaded through the executor.
    """

    def permits(self, metadata: ToolMetadata, context: ToolExecutionContext) -> bool: ...


class AllowEnabledTools:
    """The default policy: availability is decided by the tool's own flag.

    Tenancy is not re-decided here - the context only exists because the
    membership layer already established it.
    """

    def permits(self, metadata: ToolMetadata, context: ToolExecutionContext) -> bool:
        del context
        return metadata.enabled


class ToolExecutor:
    """Runs one tool, once, under the framework's rules."""

    def __init__(
        self,
        registry: ToolRegistry,
        settings: Settings,
        *,
        default_timeout_seconds: float | None = None,
        policy: ToolPolicy | None = None,
        instruments: Instruments | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._policy = policy or AllowEnabledTools()
        # The framework measures executions; a tool does not measure itself.
        # Keeping it here is what stops a business tool ever importing a
        # metrics module.
        self._instruments = instruments or NullInstruments()
        self._default_timeout = (
            default_timeout_seconds
            if default_timeout_seconds is not None
            else settings.tool_timeout_seconds
        )

    async def execute(
        self,
        request: ToolRequest,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        request_id: str | None = None,
        cancellation: CancellationToken = NEVER_CANCELLED,
        approval: ApprovalGrant | None = None,
    ) -> ToolResult:
        """Run the requested tool and describe what happened.

        Identity is taken as explicit keyword arguments rather than read from
        anywhere near *request*, so there is no expression a caller could write
        that would take the tenant from the model's output.

        *approval* carries a decision a person already made, and is the only way
        a tool marked ``requires_approval`` ever reaches its body. It is never
        constructed here and never derived from the request: the approval
        service builds it from a durable row that an authenticated administrator
        wrote. A refusal supplied this way comes back as a ``REJECTED`` result -
        a decision, not a fault.

        Never raises for a tool-level problem: every outcome comes back as a
        `ToolResult`.
        """
        # A resumed execution keeps the identity it was refused under, so the
        # conversation, the audit trail and the at-most-once record all name the
        # same execution. Otherwise a fresh one, assigned before anything can
        # fail so even a rejected call is identifiable in the logs.
        execution_id = approval.tool_execution_id if approval else uuid.uuid4()
        started = perf_counter()

        context = ToolExecutionContext(
            tool_execution_id=execution_id,
            organization_id=organization_id,
            user_id=user_id,
            run_id=run_id,
            agent_id=agent_id,
            request_id=request_id,
        )

        metadata: ToolMetadata | None = None

        try:
            tool = self._registry.resolve(request.tool_name)
            metadata = tool.metadata

            self._check_enabled(metadata)
            self._check_permitted(metadata, context)

            arguments = self._validate_arguments(tool, request)

            self._check_approval(metadata, approval)
            self._check_cancelled(cancellation)

            output = await self._run(tool, arguments, context)

            # After execution as well: work that finished while somebody was
            # stopping the run is still work that should not be reported as an
            # answer. The side effect has happened either way - that is the
            # honest limit of cooperative cancellation, and the log records it.
            self._check_cancelled(cancellation)

            data = self._validate_result(tool, output)

        except ToolError as exc:
            result = self._failure_result(execution_id, request.tool_name, exc, elapsed_ms(started))
            self._log(result, context, metadata, arguments_count=len(request.arguments))
            return result

        result = ToolResult(
            tool_execution_id=execution_id,
            tool_name=request.tool_name,
            outcome=ToolOutcome.SUCCEEDED,
            data=data,
            duration_ms=elapsed_ms(started),
        )
        self._log(result, context, metadata, arguments_count=len(request.arguments))
        return result

    # -- The steps -------------------------------------------------------------

    def _check_enabled(self, metadata: ToolMetadata) -> None:
        if not metadata.enabled:
            raise ToolDisabledError()

    def _check_permitted(self, metadata: ToolMetadata, context: ToolExecutionContext) -> None:
        """Whether this tool may be reached in this context.

        Not a second authorization system. Who the caller is and which
        organization they act for was settled by the membership layer, and a
        context only exists because that succeeded. What is left is whether the
        *tool* is available here - which is where per-organization enablement
        will go when real tools need it.
        """
        if not self._policy.permits(metadata, context):
            raise ToolAuthorizationError()

    def _validate_arguments(self, tool: AnyTool, request: ToolRequest) -> BaseModel:
        """Fit the model's arguments to the tool's schema, or refuse them.

        Two separate defences. Reserved names are rejected outright, whatever
        the tool declared, so identity can never arrive as an argument. Then
        Pydantic enforces the schema itself - required fields, types, lengths,
        collection sizes, allowed values, nested shapes - and a failure becomes
        a controlled error rather than a traceback.
        """
        smuggled = set(request.arguments) & RESERVED_ARGUMENT_NAMES
        if smuggled:
            raise ToolValidationError(
                "The tool was called with arguments it cannot accept.",
                details={"rejected_arguments": sorted(smuggled)},
            )

        try:
            return tool.input_model.model_validate(request.arguments)
        except ValidationError as exc:
            # Field names and constraint names only. The values are the
            # caller's data and have no business in an error payload.
            raise ToolValidationError(details={"fields": _field_errors(exc)}) from exc

    def _check_approval(self, metadata: ToolMetadata, approval: ApprovalGrant | None) -> None:
        """Nothing marked for approval runs without a decision in hand.

        Three outcomes, and the default is the strict one:

        * no decision -> stopped, and the caller is told approval is required;
        * a refusal   -> stopped, and reported as a rejection rather than a
          failure, because a person decided it;
        * a grant     -> allowed through, and only then.

        Placed after validation so an approval-required tool called wrongly
        reports the wrong arguments rather than hiding them behind a request
        nobody could have granted.

        A grant for a tool that does not need one is ignored rather than
        honoured as anything: it grants nothing that was not already allowed.
        """
        if not metadata.requires_approval:
            return

        if approval is None:
            raise ToolApprovalRequiredError()

        if not approval.granted:
            raise ToolApprovalRejectedError()

    def _check_cancelled(self, cancellation: CancellationToken) -> None:
        if cancellation.cancelled:
            raise _Cancelled()

    async def _run(
        self, tool: AnyTool, arguments: BaseModel, context: ToolExecutionContext
    ) -> object:
        """Execute the tool body under a deadline.

        The deadline is the framework's, never the tool's to waive: a tool that
        forgot its own timeout would otherwise hold a request open for as long
        as whatever it is talking to feels like.

        **Nothing is retried.** A tool may send an email or charge money, and
        the executor cannot tell from here whether a failure happened before or
        after the side effect. The gateway's retry policy covers model calls
        and stops there.
        """
        timeout = tool.metadata.timeout_seconds or self._default_timeout

        try:
            async with asyncio.timeout(timeout):
                return await tool.execute(arguments, context)
        except TimeoutError as exc:
            raise ToolTimeoutError() from exc
        except ToolError:
            # The tool was specific about its own failure; that is allowed and
            # its message was written for a client.
            raise
        except Exception as exc:
            # Everything else is replaced. A third-party exception routinely
            # carries a hostname, a query or a key in its text.
            logger.exception(
                "Tool raised an unhandled exception",
                extra={
                    "context": {
                        "tool": tool.metadata.name,
                        "error": type(exc).__name__,
                    }
                },
            )
            raise ToolExecutionError() from exc

    def _validate_result(self, tool: AnyTool, output: object) -> dict[str, object]:
        """Check what the tool returned against its own output schema.

        Output is untrusted too, from the framework's point of view: a tool
        that returns the wrong shape should fail here rather than put an
        arbitrary Python object into an agent run.
        """
        try:
            validated = tool.output_model.model_validate(output)
        except ValidationError as exc:
            raise ToolInvalidResultError(details={"fields": _field_errors(exc)}) from exc

        data: dict[str, object] = validated.model_dump(mode="json")

        size = len(json.dumps(data))
        if size > self._settings.tool_max_result_bytes:
            raise ToolInvalidResultError(
                "The tool returned a result that could not be used.",
                details={
                    "reason": "result_too_large",
                    "bytes": size,
                    "limit": self._settings.tool_max_result_bytes,
                },
            )

        return data

    # -- Results and logging ---------------------------------------------------

    def _failure_result(
        self,
        execution_id: uuid.UUID,
        tool_name: str,
        exc: ToolError,
        duration_ms: float,
    ) -> ToolResult:
        if isinstance(exc, _Cancelled):
            return ToolResult(
                tool_execution_id=execution_id,
                tool_name=tool_name,
                outcome=ToolOutcome.CANCELLED,
                failure=ToolFailure(code="tool_cancelled", message="The tool run was cancelled."),
                duration_ms=duration_ms,
            )

        outcome = _OUTCOME_BY_ERROR.get(type(exc), ToolOutcome.FAILED)

        return ToolResult(
            tool_execution_id=execution_id,
            tool_name=tool_name,
            outcome=outcome,
            failure=ToolFailure(code=exc.code, message=exc.message, details=exc.details),
            duration_ms=duration_ms,
        )

    def _log(
        self,
        result: ToolResult,
        context: ToolExecutionContext,
        metadata: ToolMetadata | None,
        *,
        arguments_count: int,
    ) -> None:
        """Operational metadata only.

        Deliberately absent: the arguments, the result, and anything derived
        from either. A tool's input and output are the tenant's business data,
        and a log file is the wrong place for a customer record.
        """
        # Recorded beside the log, from the same values, so the two cannot
        # disagree about what happened. The tool name is bounded by the
        # registry; the outcome and the safety class are enums.
        self._instruments.record_tool_execution(
            tool=result.tool_name,
            outcome=result.outcome.value,
            safety=metadata.safety.value if metadata else None,
            milliseconds=result.duration_ms,
        )

        logger.info(
            "Tool execution finished",
            extra={
                "context": {
                    "tool_execution_id": str(result.tool_execution_id),
                    "request_id": context.request_id,
                    "run_id": str(context.run_id) if context.run_id else None,
                    "agent_id": str(context.agent_id) if context.agent_id else None,
                    "organization_id": str(context.organization_id),
                    "user_id": str(context.user_id),
                    "tool": result.tool_name,
                    "safety": metadata.safety.value if metadata else None,
                    "outcome": result.outcome.value,
                    "error_code": result.failure.code if result.failure else None,
                    "duration_ms": round(result.duration_ms, 2),
                    "argument_count": arguments_count,
                    "result_fields": len(result.data) if result.data else 0,
                }
            },
        )


class _Cancelled(ToolError):
    """Internal marker. Never leaves this module: it becomes a CANCELLED result."""

    code = "tool_cancelled"
    message = "The tool run was cancelled."


def _field_errors(exc: ValidationError) -> list[str]:
    """Which fields were wrong, and how - never what they contained."""
    return sorted(
        {
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in exc.errors()
        }
    )


def elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000
