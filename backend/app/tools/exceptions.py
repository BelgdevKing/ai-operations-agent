"""Failures that belong to tool execution.

Each one answers a different question for whoever is debugging: was the tool
missing, switched off, not permitted here, called wrongly, broken, too slow, or
waiting on a person? Collapsing them into one error would lose exactly the
distinction that makes a failed run diagnosable.

Two rules hold for every class below. The message a client sees is written
here, so a third-party library's exception text, a connection string or a file
path can never become one; and the status codes stay inside the existing
application error boundary rather than starting a second one.
"""

from __future__ import annotations

from app.core.exceptions import AppError


class ToolError(AppError):
    """Base class for tool failures."""

    status_code = 500
    code = "tool_error"
    message = "The tool could not be run."


class ToolNotFoundError(ToolError):
    """No tool is registered under that name.

    Ordinary rather than alarming: the name came from a model, and a model can
    ask for something that does not exist.
    """

    status_code = 404
    code = "tool_not_found"
    message = "That tool is not available."


class ToolDisabledError(ToolError):
    """The tool exists but has been switched off."""

    status_code = 409
    code = "tool_disabled"
    message = "That tool is currently disabled."


class ToolAuthorizationError(ToolError):
    """The tool exists and runs, but not for this execution context.

    Framework-level only. Who the caller is and which organization they belong
    to was settled by the membership layer long before anything reached here.
    """

    status_code = 403
    code = "tool_not_permitted"
    message = "That tool is not permitted here."


class ToolValidationError(ToolError):
    """The arguments do not fit the tool's declared input schema.

    A 422 because the call was malformed - which, for a tool, means the model
    got the arguments wrong rather than that anything is broken.
    """

    status_code = 422
    code = "tool_invalid_arguments"
    message = "The tool was called with arguments it cannot accept."


class ToolExecutionError(ToolError):
    """The tool ran and failed.

    Whatever it raised is logged and replaced here. A tool talks to systems
    whose exceptions routinely carry hostnames, credentials and queries, and
    none of that belongs in a response.
    """

    status_code = 502
    code = "tool_execution_failed"
    message = "The tool failed while running."


class ToolTimeoutError(ToolError):
    """The tool exceeded its deadline.

    Deliberately distinct from a failure: the tool may well have completed its
    side effect after the framework stopped waiting, which is a different thing
    to tell a user than "it did not work".
    """

    status_code = 504
    code = "tool_timeout"
    message = "The tool took too long and was stopped."


class ToolApprovalRequiredError(ToolError):
    """The tool needs a person to agree before it runs.

    Not an error in the usual sense - nothing went wrong, and the tool was not
    executed. It is an exception so that no execution path can reach a tool
    body without passing this check first.
    """

    status_code = 409
    code = "tool_approval_required"
    message = "That tool needs approval before it can run."


class ToolApprovalRejectedError(ToolError):
    """A person was asked and said no.

    A 403 rather than a 5xx: the request was understood, the action was
    possible, and somebody with the authority to decide declined it. The agent
    is told plainly so it can say so, rather than reporting that something
    failed.
    """

    status_code = 403
    code = "tool_approval_rejected"
    message = "A person declined this action, so it was not performed."


class ToolInvalidResultError(ToolError):
    """The tool returned something its own output schema does not describe.

    A bug in the tool rather than in the call, and caught here so the object
    never reaches the agent runtime.
    """

    status_code = 502
    code = "tool_invalid_result"
    message = "The tool returned a result that could not be used."


class ToolRegistrationError(ToolError):
    """A tool was declared wrongly, or registered twice.

    Raised at start-up rather than during a run: a registry with two tools
    answering to one name has no correct behaviour to fall back on.
    """

    status_code = 500
    code = "tool_registration_error"
    message = "The tool registry is misconfigured."
