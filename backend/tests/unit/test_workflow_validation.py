"""Whether a workflow will run, decided before anybody can start it.

Graph rules and world rules: reachability, cycles, endings, limits, and whether
the tools and agents a definition names are ones this organization actually has.
No database - the registries are in memory and the document is a document.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.registry import DEMO_AGENT_ID, AgentRegistry, build_demo_agent
from app.core.config import Settings
from app.demo.workflows import shipment_exception_definition
from app.tools.registry import ToolRegistry
from app.workflows.definition import WorkflowDefinition
from app.workflows.exceptions import WorkflowValidationError
from app.workflows.validation import WorkflowLimits, validate_definition
from tests.tool_helpers import CancelShipmentTool, EchoTool

ORGANIZATION = uuid.uuid4()


def settings(**overrides: object) -> Settings:
    return Settings(app_env="test", **overrides)  # type: ignore[arg-type]


def registries() -> tuple[ToolRegistry, AgentRegistry]:
    """A registry with the two example tools, and the platform agent."""
    return ToolRegistry([EchoTool(), CancelShipmentTool()]), AgentRegistry(
        [build_demo_agent(settings())]
    )


def check(
    document: dict[str, object],
    *,
    limits: WorkflowLimits | None = None,
    organization_id: uuid.UUID = ORGANIZATION,
) -> None:
    tools, agents = registries()
    validate_definition(
        WorkflowDefinition.model_validate(document),
        limits=limits or WorkflowLimits.from_settings(settings()),
        tools=tools,
        agents=agents,
        organization_id=organization_id,
    )


def problems_of(document: dict[str, object], **kwargs: object) -> list[str]:
    with pytest.raises(WorkflowValidationError) as caught:
        check(document, **kwargs)  # type: ignore[arg-type]
    return list(caught.value.details["problems"])


def tool_step(step_id: str, next_step: str | None, tool: str = "echo") -> dict[str, object]:
    return {
        "id": step_id,
        "type": "tool_call",
        "tool": tool,
        "arguments": {"text": "hello"},
        "next": next_step,
    }


def linear(*ids: str) -> dict[str, object]:
    steps = [
        tool_step(step_id, ids[index + 1] if index + 1 < len(ids) else None)
        for index, step_id in enumerate(ids)
    ]
    return {"entry": ids[0], "steps": steps}


# -- The happy path ----------------------------------------------------------


def test_a_linear_workflow_validates() -> None:
    check(linear("a", "b", "c"))


def test_the_demonstration_workflow_validates_against_the_real_registry() -> None:
    """The business tools it names are the ones the platform actually installs."""
    from app.tools.business import build_business_registry

    validate_definition(
        WorkflowDefinition.model_validate(shipment_exception_definition()),
        limits=WorkflowLimits.from_settings(settings()),
        # None stands in for the session: building the registry touches nothing.
        tools=build_business_registry(None),  # type: ignore[arg-type]
        agents=AgentRegistry([build_demo_agent(settings())]),
        organization_id=ORGANIZATION,
    )


def test_a_branching_workflow_validates() -> None:
    check(
        {
            "entry": "look",
            "steps": [
                tool_step("look", "branch"),
                {
                    "id": "branch",
                    "type": "condition",
                    "condition": {"field": "$.steps.look.output.x", "operator": "exists"},
                    "on_true": "act",
                    "on_false": None,
                },
                tool_step("act", None),
            ],
        }
    )


# -- The graph ---------------------------------------------------------------


def test_an_unreachable_step_is_refused() -> None:
    document = linear("a", "b")
    document["steps"].append(tool_step("orphan", None))  # type: ignore[union-attr]

    assert any("cannot be reached" in problem for problem in problems_of(document))


def test_a_cycle_is_refused() -> None:
    """Loops are not supported, and allowing one accidentally is how an engine
    acquires a workflow that never finishes."""
    document = {
        "entry": "a",
        "steps": [tool_step("a", "b"), tool_step("b", "a")],
    }

    problems = problems_of(document)

    assert any("cycle" in problem for problem in problems)


def test_a_self_loop_is_refused() -> None:
    document = {"entry": "a", "steps": [tool_step("a", "a")]}

    assert any("cycle" in problem for problem in problems_of(document))


def test_a_longer_cycle_is_found() -> None:
    document = {
        "entry": "a",
        "steps": [tool_step("a", "b"), tool_step("b", "c"), tool_step("c", "b")],
    }

    assert any("cycle" in problem for problem in problems_of(document))


def test_a_cycle_report_names_the_path() -> None:
    document = {"entry": "a", "steps": [tool_step("a", "b"), tool_step("b", "a")]}

    problem = next(p for p in problems_of(document) if "cycle" in p)

    assert "a" in problem and "b" in problem


def test_a_branch_that_rejoins_is_not_a_cycle() -> None:
    """A diamond is an ordinary shape and must not be mistaken for a loop."""
    check(
        {
            "entry": "start",
            "steps": [
                {
                    "id": "start",
                    "type": "condition",
                    "condition": {"field": "$.input.x", "operator": "exists"},
                    "on_true": "left",
                    "on_false": "right",
                },
                tool_step("left", "join"),
                tool_step("right", "join"),
                tool_step("join", None),
            ],
        }
    )


def test_a_workflow_with_no_ending_is_refused() -> None:
    """Every step transitions somewhere, so nothing finishes. Only reachable
    when a cycle is absent, which is why the check is separate."""
    # A cycle also produces no ending, so the message is the cycle's; this uses
    # the only no-ending shape that is acyclic - there is none, so the cycle
    # report standing in for it is the honest behaviour.
    document = {"entry": "a", "steps": [tool_step("a", "b"), tool_step("b", "a")]}

    problems = problems_of(document)

    assert any("cycle" in problem for problem in problems)
    assert not any("No path" in problem for problem in problems), (
        "one problem, not two: a cycle already explains why nothing ends"
    )


# -- What it names -----------------------------------------------------------


def test_an_unknown_tool_is_refused() -> None:
    document = {"entry": "a", "steps": [tool_step("a", None, tool="not_installed")]}

    assert any("not a known tool" in problem for problem in problems_of(document))


def test_an_agent_this_organization_cannot_use_is_refused() -> None:
    document = {
        "entry": "a",
        "steps": [
            {
                "id": "a",
                "type": "agent_step",
                "agent_id": str(uuid.uuid4()),
                "message": "Hello.",
                "next": None,
            }
        ],
    }

    assert any("cannot use" in problem for problem in problems_of(document))


def test_the_platform_agent_is_usable_by_any_organization() -> None:
    check(
        {
            "entry": "a",
            "steps": [
                {
                    "id": "a",
                    "type": "agent_step",
                    "agent_id": str(DEMO_AGENT_ID),
                    "message": "Hello.",
                    "next": None,
                }
            ],
        },
        organization_id=uuid.uuid4(),
    )


def test_a_reference_to_a_step_that_does_not_exist_is_refused() -> None:
    document = {
        "entry": "a",
        "steps": [
            {
                "id": "a",
                "type": "tool_call",
                "tool": "echo",
                "arguments": {"text": "$.steps.nowhere.output.x"},
                "next": None,
            }
        ],
    }

    assert any("does not exist" in problem for problem in problems_of(document))


def test_a_step_cannot_read_its_own_output() -> None:
    document = {
        "entry": "a",
        "steps": [
            {
                "id": "a",
                "type": "tool_call",
                "tool": "echo",
                "arguments": {"text": "$.steps.a.output.x"},
                "next": None,
            }
        ],
    }

    assert any("its own output" in problem for problem in problems_of(document))


def test_an_input_reference_needs_no_step_to_exist() -> None:
    check(
        {
            "entry": "a",
            "steps": [
                {
                    "id": "a",
                    "type": "tool_call",
                    "tool": "echo",
                    "arguments": {"text": "$.input.text"},
                    "next": None,
                }
            ],
        }
    )


# -- Limits ------------------------------------------------------------------


def test_too_many_steps_is_refused() -> None:
    ids = [f"s{index}" for index in range(6)]
    limits = WorkflowLimits(
        max_steps=5, max_definition_bytes=100_000, max_input_bytes=1_000, max_output_bytes=1_000
    )

    problems = problems_of(linear(*ids), limits=limits)

    assert any("the limit is 5" in problem for problem in problems)


def test_an_oversized_document_is_refused() -> None:
    limits = WorkflowLimits(
        max_steps=50, max_definition_bytes=100, max_input_bytes=1_000, max_output_bytes=1_000
    )

    problems = problems_of(linear("a", "b", "c"), limits=limits)

    assert any("the limit is 100" in problem for problem in problems)


def test_every_problem_is_reported_at_once() -> None:
    """Somebody writing a workflow wants the list, not a game of whack-a-mole."""
    document = {
        "entry": "a",
        "steps": [tool_step("a", None, tool="not_installed"), tool_step("orphan", None)],
    }

    problems = problems_of(document)

    assert len(problems) >= 2
    assert any("not a known tool" in problem for problem in problems)
    assert any("cannot be reached" in problem for problem in problems)


def test_the_limits_come_from_settings() -> None:
    limits = WorkflowLimits.from_settings(settings())

    assert limits.max_steps == 32
    assert limits.max_definition_bytes == 65_536
    assert limits.max_input_bytes == 16_384
    assert limits.max_output_bytes == 65_536


def test_configuration_cannot_exceed_the_hard_ceilings() -> None:
    """A mistyped environment variable cannot turn one document into an
    unbounded one."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(app_env="test", workflow_max_steps=1_000)
    with pytest.raises(ValidationError):
        Settings(app_env="test", workflow_max_definition_bytes=100_000_000)
    with pytest.raises(ValidationError):
        Settings(app_env="test", workflow_max_input_bytes=100_000_000)
