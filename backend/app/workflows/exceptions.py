"""Failures that belong to the workflow engine.

Not a second error hierarchy. Everything the agent runtime, the tool framework
and the provider gateway already normalise travels up unchanged with its own
status code and its own meaning; what lives here is the vocabulary the workflow
layer needs for things only it can get wrong - a definition that will not run, a
graph with no way out, a step that cannot be resumed.

Every message is written for a client. A provider's words, a driver's words, a
tool's arguments and a stack trace never appear in one; the specific detail goes
to the log, which is where somebody debugging can see it.

The distinction §16 asks for is carried by the *class*, not by a string:

    WorkflowValidationError    the definition is wrong
    WorkflowInputError         what was passed in is wrong
    WorkflowStepFailedError    a business step did not succeed
    ApprovalRejectedError      a person said no - not a fault
    WorkflowStateError         the engine was asked for an illegal transition
    WorkflowAbandonedError     nothing is driving this run any more
"""

from __future__ import annotations

from app.core.exceptions import AppError


class WorkflowError(AppError):
    """Base class for workflow failures."""

    status_code = 500
    code = "workflow_error"
    message = "The workflow could not be run."


class WorkflowNotFoundError(WorkflowError):
    """No such workflow, or none this organization may use.

    One answer for both, so that guessing an id cannot confirm that another
    tenant has a workflow by that name.
    """

    status_code = 404
    code = "workflow_not_found"
    message = "That workflow does not exist."


class WorkflowRunNotFoundError(WorkflowError):
    """No such run for this organization."""

    status_code = 404
    code = "workflow_run_not_found"
    message = "That workflow run does not exist."


class WorkflowNotActiveError(WorkflowError):
    """The workflow exists but is not runnable.

    A draft is something somebody is still writing and an inactive version is
    one somebody deliberately stopped; starting either would be the platform
    deciding on their behalf.
    """

    status_code = 409
    code = "workflow_not_active"
    message = "That workflow version is not active, so it cannot be started."


class WorkflowValidationError(WorkflowError):
    """The definition will not run, and says why.

    A 422 with details: unlike most errors here, the person reading it wrote the
    thing that is wrong, and a message that does not say which step is useless
    to them. The details name steps, operators and references - never values
    from anybody's business data, because a definition is written before there
    is any.
    """

    status_code = 422
    code = "workflow_invalid"
    message = "The workflow definition could not be validated."


class WorkflowInputError(WorkflowError):
    """The input handed to a run is not acceptable.

    Separate from a definition being invalid: the workflow is fine and this
    particular request is not, which is a different thing for a caller to fix.
    """

    status_code = 422
    code = "workflow_invalid_input"
    message = "The workflow input could not be accepted."


class WorkflowStateError(WorkflowError):
    """An illegal lifecycle transition was attempted.

    Always a bug in the engine rather than anything a caller did, so the client
    sees the generic 5xx message and the detail goes to the log.
    """

    status_code = 500
    code = "workflow_state_error"
    message = "The workflow run is not in a state that allows that."


class WorkflowRunNotCancellableError(WorkflowError):
    """The run is not in a state a person can stop.

    A 409, and the agent runtime's reasoning unchanged: cancellation ends a run
    that is *waiting*. A finished run has nothing to stop, and a running one is
    being advanced by another request.
    """

    status_code = 409
    code = "workflow_run_not_cancellable"
    message = "That run is not waiting for approval, so it cannot be cancelled."


class WorkflowStepFailedError(WorkflowError):
    """A step did not succeed, and the run stops.

    A 502 because the thing that failed was downstream of the engine - a tool,
    an agent, a record that was not there. The run records the step and a stable
    code; what actually went wrong is in the step's own error code, which is the
    tool framework's or the runtime's and was already sanitised by them.
    """

    status_code = 502
    code = "workflow_step_failed"
    message = "A workflow step did not succeed, so the workflow stopped."


class WorkflowReferenceError(WorkflowError):
    """A step read something that is not there.

    Distinct from a validation failure: the reference was well-formed and named
    a real step, and at run time that step's output did not contain the path -
    because a branch skipped it, or because a tool returned a different shape
    than the workflow's author expected.
    """

    status_code = 422
    code = "workflow_reference_unresolved"
    message = "A workflow step referred to a value that was not available."


class ApprovalRejectedError(WorkflowError):
    """A person declined, and the workflow had nowhere else to go.

    A 409 rather than a 5xx, and its own class rather than a failure: nothing
    is broken, nothing should be retried, and a process that records a human
    refusal as an infrastructure fault teaches everyone to ignore its alerts.
    """

    status_code = 409
    code = "workflow_approval_rejected"
    message = "A person declined this action, and the workflow had no path for a refusal."


class WorkflowCancelledError(WorkflowError):
    """The run was stopped before it finished.

    499 is nginx's "client closed request": not a server fault, and not
    something to retry blindly.
    """

    status_code = 499
    code = "workflow_cancelled"
    message = "The workflow run was cancelled."


class WorkflowStepLimitError(WorkflowError):
    """The run executed more steps than it is allowed to.

    A graph is validated acyclic, so this should be unreachable. It exists
    because "should be unreachable" is not the same as "is", and an engine that
    could loop is worse than one that stops and says so.
    """

    status_code = 500
    code = "workflow_step_limit_exceeded"
    message = "The workflow did not finish within its step limit."


class WorkflowOutputTooLargeError(WorkflowError):
    """A step produced more than the workflow may carry.

    Refused rather than truncated: a silently shortened result is one a later
    step would branch on without anybody knowing it was incomplete.
    """

    status_code = 413
    code = "workflow_output_too_large"
    message = "A workflow step produced a result too large to carry."
