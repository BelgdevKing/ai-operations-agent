"""The metric vocabularies, checked against the enums they mirror.

``app/observability/names.py`` writes its label vocabularies out as literals
rather than deriving them from the enums, because deriving them makes the
observability package import the agent runtime, the tool framework and the
models - and the first version of that file failed to start for exactly that
reason. Observability is imported *by* those layers; importing them back is a
cycle.

This file is what pays for the duplication. Every set is asserted equal to the
enum it names, so adding a lifecycle state and forgetting to list it fails here
rather than silently landing in the ``other`` bucket months later.
"""

from __future__ import annotations

from app.agents.models import AgentRunStatus
from app.models.enums import ApprovalStatus, RunStatus, StepRunStatus, WorkflowStepType
from app.observability import names
from app.tools.models import ToolOutcome, ToolSafety


def values(enum_class: type) -> set[str]:
    return {str(member.value) for member in enum_class}


def test_agent_run_statuses_match_the_runtime() -> None:
    assert names.AGENT_RUN_STATUSES == values(AgentRunStatus)


def test_workflow_run_statuses_match_the_schema() -> None:
    assert names.WORKFLOW_RUN_STATUSES == values(RunStatus)


def test_workflow_step_statuses_match_the_schema() -> None:
    assert names.WORKFLOW_STEP_STATUSES == values(StepRunStatus)


def test_workflow_step_types_match_the_schema() -> None:
    assert names.WORKFLOW_STEP_TYPES == values(WorkflowStepType)


def test_approval_decisions_match_the_schema() -> None:
    assert names.APPROVAL_DECISIONS_SET == values(ApprovalStatus)


def test_tool_outcomes_match_the_framework() -> None:
    assert names.TOOL_OUTCOMES == values(ToolOutcome)


def test_tool_safety_matches_the_framework_plus_unknown() -> None:
    """Plus one: a tool the registry has no metadata for still gets executed
    and still gets counted, and "unknown" is what it is labelled."""
    assert names.TOOL_SAFETY == values(ToolSafety) | {"unknown"}


def test_the_observability_package_imports_nothing_from_the_application() -> None:
    """The rule that made the literals necessary, asserted rather than trusted.

    ``names`` is the leaf: if it ever imports the agent runtime or the models
    again, the cycle comes back and the application stops booting - which is a
    worse way to find out than this.
    """
    import ast
    from pathlib import Path

    source = Path(names.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    from_app = {module for module in imported if module.startswith("app.")}
    assert from_app == set(), f"names.py must stay a leaf; it imports {from_app}"
