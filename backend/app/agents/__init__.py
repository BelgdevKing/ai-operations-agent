"""Agent execution.

The runtime that turns a request into a controlled agent run:

    AgentRuntime -> AgentRunner -> LLMGateway -> provider

It decides what the agent is allowed to do, how many model calls it may make,
and what it is allowed to return. It does not choose a provider, retry, or
touch a vendor SDK - that is the gateway's, and this package imports no
provider code at all.

Part 12 is the foundation only. An agent can answer, or it can say which tool
it would need; **no tool is executed**, and a tool request ends the run as the
handoff point for the tool framework.

Nothing here is persisted. See :mod:`app.agents.models` for why.
"""

from app.agents.cancellation import NEVER_CANCELLED, Cancellation, CancellationToken
from app.agents.decisions import (
    AgentDecision,
    AgentDecisionEnvelope,
    DecisionType,
    FinalDecision,
    ToolRequestDecision,
)
from app.agents.exceptions import (
    AgentCancelledError,
    AgentConfigurationError,
    AgentDisabledError,
    AgentError,
    AgentInvalidDecisionError,
    AgentMaxStepsExceededError,
    AgentNotFoundError,
    AgentRunError,
    AgentStateError,
)
from app.agents.models import (
    Agent,
    AgentContext,
    AgentRun,
    AgentRunStatus,
    AgentStep,
)
from app.agents.registry import DEMO_AGENT_ID, AgentRegistry, build_demo_agent
from app.agents.runner import AgentRunner
from app.agents.runtime import AgentRuntime

__all__ = [
    "DEMO_AGENT_ID",
    "NEVER_CANCELLED",
    "Agent",
    "AgentCancelledError",
    "AgentConfigurationError",
    "AgentContext",
    "AgentDecision",
    "AgentDecisionEnvelope",
    "AgentDisabledError",
    "AgentError",
    "AgentInvalidDecisionError",
    "AgentMaxStepsExceededError",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentRun",
    "AgentRunError",
    "AgentRunStatus",
    "AgentRunner",
    "AgentRuntime",
    "AgentStateError",
    "AgentStep",
    "Cancellation",
    "CancellationToken",
    "DecisionType",
    "FinalDecision",
    "ToolRequestDecision",
    "build_demo_agent",
]
