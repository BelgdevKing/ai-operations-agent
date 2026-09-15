"""Whether a workflow will run, decided before anybody starts it.

Structural rules live on the document (:mod:`app.workflows.definition`). What
lives here is everything that needs to know about the world the workflow would
run in - which tools are installed, which agents this organization may use, and
what the deployment's limits are - plus the graph rules, which need the whole
document at once.

    entry exists ─▶ every transition lands ─▶ every step reachable
        ─▶ no cycles ─▶ some path ends ─▶ within the limits

Validation happens at **activation**, not at start. A workflow is written as a
draft, checked once, and then runs many times; checking on every start would
spend the same work repeatedly and - worse - would let a definition that was
fine yesterday be started today when a tool it names has been removed. Checking
at activation means the answer is recorded, and removing a tool is a change
somebody has to notice.

Every problem is collected rather than raised on the first one. Somebody writing
a workflow wants the list, not a game of whack-a-mole.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from app.agents.registry import AgentRegistry
from app.core.config import Settings
from app.tools.registry import ToolRegistry
from app.workflows.definition import WorkflowDefinition
from app.workflows.exceptions import WorkflowValidationError
from app.workflows.references import ReferenceRoot


@dataclass(frozen=True)
class WorkflowLimits:
    """What a definition may not exceed.

    From configuration, with hard ceilings in :mod:`app.core.config`. A
    deployment can be stricter; it cannot be more permissive, so a mistyped
    environment variable cannot turn one workflow into an unbounded one.
    """

    max_steps: int
    max_definition_bytes: int
    max_input_bytes: int
    max_output_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkflowLimits:
        return cls(
            max_steps=settings.workflow_max_steps,
            max_definition_bytes=settings.workflow_max_definition_bytes,
            max_input_bytes=settings.workflow_max_input_bytes,
            max_output_bytes=settings.workflow_max_output_bytes,
        )


def validate_definition(
    definition: WorkflowDefinition,
    *,
    limits: WorkflowLimits,
    tools: ToolRegistry,
    agents: AgentRegistry,
    organization_id: uuid.UUID,
) -> None:
    """Refuse a definition that cannot safely be activated.

    *organization_id* is the caller's verified tenant, and it is what an agent
    reference is checked against - so a workflow cannot be activated against an
    agent belonging to somebody else, and activating it does not disclose
    whether such an agent exists.

    Raises:
        WorkflowValidationError: With every problem found, as ``details``.
    """
    problems: list[str] = []

    problems += _size_problems(definition, limits)
    problems += _capability_problems(definition, tools, agents, organization_id)
    problems += _reference_problems(definition)
    problems += _graph_problems(definition)

    if problems:
        raise WorkflowValidationError(details={"problems": sorted(set(problems))})


# -- Size ---------------------------------------------------------------------


def _size_problems(definition: WorkflowDefinition, limits: WorkflowLimits) -> list[str]:
    problems: list[str] = []

    if len(definition.steps) > limits.max_steps:
        problems.append(
            f"The workflow has {len(definition.steps)} steps; the limit is {limits.max_steps}."
        )

    encoded = len(json.dumps(definition.model_dump(mode="json")).encode("utf-8"))
    if encoded > limits.max_definition_bytes:
        problems.append(
            f"The definition is {encoded} bytes; the limit is {limits.max_definition_bytes}."
        )

    return problems


# -- What it names ------------------------------------------------------------


def _capability_problems(
    definition: WorkflowDefinition,
    tools: ToolRegistry,
    agents: AgentRegistry,
    organization_id: uuid.UUID,
) -> list[str]:
    problems: list[str] = []
    visible = {str(agent.id) for agent in agents.list_for(organization_id)}

    for step in definition.steps:
        tool_name = getattr(step, "tool", None)
        if tool_name is not None and not tools.has(tool_name):
            problems.append(f"Step {step.id!r} calls {tool_name!r}, which is not a known tool.")

        agent_id = getattr(step, "agent_id", None)
        if agent_id is not None and agent_id not in visible:
            # The same answer whether the agent does not exist or belongs to
            # another organization, for the reason every scoped lookup uses.
            problems.append(
                f"Step {step.id!r} runs agent {agent_id!r}, which this organization cannot use."
            )

    return problems


# -- References ---------------------------------------------------------------


def _reference_problems(definition: WorkflowDefinition) -> list[str]:
    """Every step reference must name a step that exists and is not itself.

    Whether that step will actually have produced output by the time this one
    runs is a question about paths rather than about the document - a branch may
    legitimately skip it - so it is answered at run time, with a controlled
    ``workflow_reference_unresolved`` rather than a guess.
    """
    known = set(definition.by_id)
    problems: list[str] = []

    for step in definition.steps:
        for reference in step.references:
            if reference.root is not ReferenceRoot.STEPS:
                continue
            if reference.step_id == step.id:
                problems.append(f"Step {step.id!r} refers to its own output.")
            elif reference.step_id not in known:
                problems.append(
                    f"Step {step.id!r} refers to {reference.step_id!r}, which does not exist."
                )

    return problems


# -- The graph ----------------------------------------------------------------


def _graph_problems(definition: WorkflowDefinition) -> list[str]:
    problems: list[str] = []

    reachable = _reachable_from(definition, definition.entry)

    orphans = set(definition.by_id) - reachable
    if orphans:
        problems.append(
            "These steps cannot be reached from the entry step: " + ", ".join(sorted(orphans)) + "."
        )

    cycle = _find_cycle(definition)
    if cycle is not None:
        # Loops are not supported, and allowing one accidentally is how an
        # engine acquires a workflow that never finishes. If they are ever
        # wanted they should arrive as a bounded feature, deliberately.
        problems.append("The workflow contains a cycle: " + " -> ".join(cycle) + ".")

    if cycle is None and not _has_terminal_path(definition, reachable):
        problems.append("No path through the workflow ends.")

    return problems


def _reachable_from(definition: WorkflowDefinition, start: str) -> set[str]:
    steps = definition.by_id
    seen: set[str] = set()
    pending = [start]

    while pending:
        current = pending.pop()
        if current in seen or current not in steps:
            continue
        seen.add(current)
        pending.extend(steps[current].transitions)

    return seen


def _find_cycle(definition: WorkflowDefinition) -> list[str] | None:
    """The first cycle found, as the path around it.

    An iterative depth-first walk with three colours - unvisited, on the current
    path, finished. Iterative rather than recursive because the step limit is
    configuration and a deployment that raised it should not be able to exhaust
    the Python stack.
    """
    steps = definition.by_id
    finished: set[str] = set()
    on_path: list[str] = []
    on_path_set: set[str] = set()

    # Each frame is a step and the transitions of it still to explore.
    stack: list[tuple[str, list[str]]] = [
        (definition.entry, list(steps[definition.entry].transitions))
    ]
    on_path.append(definition.entry)
    on_path_set.add(definition.entry)

    while stack:
        current, remaining = stack[-1]

        if not remaining:
            stack.pop()
            finished.add(current)
            on_path.pop()
            on_path_set.discard(current)
            continue

        target = remaining.pop()

        if target in on_path_set:
            start = on_path.index(target)
            return [*on_path[start:], target]

        if target in finished or target not in steps:
            continue

        on_path.append(target)
        on_path_set.add(target)
        stack.append((target, list(steps[target].transitions)))

    return None


def _has_terminal_path(definition: WorkflowDefinition, reachable: Iterable[str]) -> bool:
    """Whether some reachable step ends the workflow.

    A step with no transitions is an ending. Called only when the graph is
    acyclic, where one such step guarantees every run finishes.
    """
    steps = definition.by_id
    return any(not steps[step_id].transitions for step_id in reachable if step_id in steps)
