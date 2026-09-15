"""What an agent decides to do next.

The one thing the model is allowed to return. Two shapes today - answer, or ask
for a tool - and the union is discriminated so a third can be added without
touching anything that already handles the first two.

Nothing here knows about a provider, and nothing here executes anything. The
LLM layer's job is to produce output matching this schema; interpreting it is
the runtime's. That split is what keeps tool support out of the provider
adapters when it arrives.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# A tool name is an identifier, not free text: it will eventually select code.
# Constrained here so an unusable name is refused at the boundary rather than
# somewhere deeper.
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

MAX_FINAL_CONTENT = 100_000


class DecisionType(enum.StrEnum):
    """The kinds of decision the runtime understands."""

    FINAL = "final"
    TOOL_REQUEST = "tool_request"


class FinalDecision(BaseModel):
    """The agent is finished and this is the answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[DecisionType.FINAL] = DecisionType.FINAL
    content: str = Field(
        min_length=1,
        max_length=MAX_FINAL_CONTENT,
        description="The answer to give the user.",
    )


class ToolRequestDecision(BaseModel):
    """The agent wants a tool run before it can answer.

    **Part 12 does not execute this.** It is recorded as the run's outcome and
    handed back, which is the boundary the tool framework will pick up.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[DecisionType.TOOL_REQUEST] = DecisionType.TOOL_REQUEST
    tool_name: str = Field(
        pattern=TOOL_NAME_PATTERN,
        description="Name of the tool the agent is asking for.",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments the agent proposes. Not validated against a "
        "tool schema yet - no tool registry exists.",
    )


AgentDecision = Annotated[FinalDecision | ToolRequestDecision, Field(discriminator="type")]
"""Either shape, told apart by ``type``."""


class AgentDecisionEnvelope(BaseModel):
    """The schema the model is actually asked to fill in.

    A wrapper rather than the bare union, because structured output has to be a
    JSON *object* at the top level: OpenAI's strict mode rejects a root-level
    union outright, and a root model is an awkward thing to ask an adapter to
    handle portably. One extra key costs nothing and works on both providers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AgentDecision = Field(description="What to do next.")
