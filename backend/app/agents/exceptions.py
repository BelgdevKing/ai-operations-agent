"""Failures that belong to the agent runtime.

Deliberately *not* a second provider error hierarchy. Anything the model or its
vendor does wrong is already normalised by :mod:`app.ai.exceptions` and travels
up unchanged - rate limits, timeouts and provider faults keep their own status
codes and their own retry semantics. What lives here is the runtime's own
vocabulary: a run that was cancelled, a step budget that ran out, output the
runtime could not interpret.

Every one carries a message written for a client. The provider's own words
never appear in any of them.
"""

from __future__ import annotations

from app.core.exceptions import AppError


class AgentError(AppError):
    """Base class for runtime failures."""

    status_code = 500
    code = "agent_error"
    message = "The agent run could not be completed."


class AgentNotFoundError(AgentError):
    """No such agent, or none this organization may use.

    One error for both, on purpose. Distinguishing "does not exist" from
    "belongs to someone else" would confirm the existence of another tenant's
    agent to anyone who could guess an id - the same reasoning the membership
    service uses for users.
    """

    status_code = 404
    code = "agent_not_found"
    message = "That agent does not exist."


class AgentDisabledError(AgentError):
    """The agent exists but has been turned off."""

    status_code = 409
    code = "agent_disabled"
    message = "That agent is disabled."


class AgentConfigurationError(AgentError):
    """The agent's own configuration makes a run impossible.

    A deployment problem rather than a caller problem, so it is a 5xx and the
    message a client sees is this class's generic one.
    """

    status_code = 500
    code = "agent_configuration_error"
    message = "The agent is not configured correctly."


class AgentRunError(AgentError):
    """A run failed for a reason the runtime itself is responsible for."""

    status_code = 500
    code = "agent_run_error"
    message = "The agent run failed."


class AgentCancelledError(AgentError):
    """The run was stopped before it produced an answer.

    499 is nginx's "client closed request": not a server fault, and not a
    failure the caller should retry blindly.
    """

    status_code = 499
    code = "agent_cancelled"
    message = "The agent run was cancelled."


class AgentMaxStepsExceededError(AgentError):
    """The run used its whole step budget without reaching an answer.

    A guard against a loop that will not end, so it is reported rather than
    silently truncated.
    """

    status_code = 500
    code = "agent_max_steps_exceeded"
    message = "The agent did not finish within its step limit."


class AgentConversationTooLargeError(AgentError):
    """The run's conversation outgrew what may be sent to a model.

    Reached by appending tool results rather than by anything the caller sent:
    the request was already bounded on the way in, and a business system can
    return far more than anyone expected. Stopping is the only safe move - a
    silently truncated conversation would push the agent's own instructions out
    of the prompt, which is a security property and not just a quality one.
    """

    status_code = 413
    code = "agent_conversation_too_large"
    message = "This conversation grew too large to continue. Start a new one."


class AgentInvalidDecisionError(AgentError):
    """The model returned something the runtime cannot act on.

    Distinct from :class:`app.ai.exceptions.LLMInvalidResponseError`, which
    means the output did not match the schema at all. This one means it did
    match and was still unusable - an empty final answer, a tool request naming
    nothing.
    """

    status_code = 502
    code = "agent_invalid_decision"
    message = "The agent produced a response that could not be used."


class AgentStateError(AgentError):
    """An illegal lifecycle transition was attempted.

    Always a bug in the runtime rather than anything a caller did, so the
    client sees the generic 5xx message and the detail goes to the log.
    """

    status_code = 500
    code = "agent_state_error"
    message = "The agent run is not in a state that allows that."
