"""The tool framework.

A controlled boundary between an agent's decision and whatever a tool actually
does:

    ToolRequest (untrusted)  ->  ToolRegistry  ->  ToolExecutor  ->  Tool
                                                        |
                                                   ToolResult

The executor owns everything that must be true of *every* tool call - schema
validation both ways, tenancy, approval, a deadline, cancellation, error
normalisation, logging without business data - so that a tool implementation
contains only its own work.

Two things this package is not. It is not provider-aware: no Anthropic or
OpenAI import appears anywhere under `app/tools/`, and turning a result into
model-readable conversation belongs to the agent runtime. And it holds no real
business tools - a production registry in this part is empty, by design.
"""

from app.tools.base import AnyTool, Tool
from app.tools.exceptions import (
    ToolApprovalRequiredError,
    ToolAuthorizationError,
    ToolDisabledError,
    ToolError,
    ToolExecutionError,
    ToolInvalidResultError,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolTimeoutError,
    ToolValidationError,
)
from app.tools.executor import AllowEnabledTools, ToolExecutor, ToolPolicy
from app.tools.models import (
    RESERVED_ARGUMENT_NAMES,
    ToolExecutionContext,
    ToolFailure,
    ToolMetadata,
    ToolOutcome,
    ToolRequest,
    ToolResult,
    ToolSafety,
)
from app.tools.registry import ToolRegistry

__all__ = [
    "RESERVED_ARGUMENT_NAMES",
    "AllowEnabledTools",
    "AnyTool",
    "Tool",
    "ToolApprovalRequiredError",
    "ToolAuthorizationError",
    "ToolDisabledError",
    "ToolError",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolFailure",
    "ToolInvalidResultError",
    "ToolMetadata",
    "ToolNotFoundError",
    "ToolOutcome",
    "ToolPolicy",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolRequest",
    "ToolResult",
    "ToolSafety",
    "ToolTimeoutError",
    "ToolValidationError",
]
