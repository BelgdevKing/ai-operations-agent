"""The engine: steps in order, data between them, and every way a run can stop.

Real tools against real business records. The assertions worth reading are the
ones about what is *durable* - a run that survives its request, a step that
cannot happen twice, a result nobody silently truncated.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.demo.workflows import shipment_exception_definition
from app.models.agent_run import ToolExecutionRecord
from app.models.enums import RunStatus, ShipmentStatus, StepRunStatus, WorkflowStepType
from app.models.workflow import WorkflowRun, WorkflowStepRun
from tests.integration.auth_helpers import register
from tests.integration.workflow_helpers import (
    REASON,
    REFERENCE,
    WORKFLOWS_URL,
    condition_step,
    lookup_workflow,
    published,
    seed,
    start,
    tool_step,
)

pytestmark = pytest.mark.integration


async def steps_of(session: AsyncSession, run_id: str) -> list[WorkflowStepRun]:
    result = await session.execute(
        select(WorkflowStepRun)
        .where(WorkflowStepRun.workflow_run_id == uuid.UUID(run_id))
        .order_by(WorkflowStepRun.position)
    )
    return list(result.scalars().all())


# -- Sequential execution -----------------------------------------------------


async def test_two_tool_steps_run_in_order(api_client: AsyncClient, session: AsyncSession) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "succeeded"
    assert run["step_count"] == 2
    assert [step["step_key"] for step in run["steps"]] == ["look", "charges"]
    assert [step["position"] for step in run["steps"]] == [1, 2]
    assert all(step["status"] == "succeeded" for step in run["steps"])


async def test_each_step_is_stored_with_what_it_was_and_when(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    stored = await steps_of(session, run["run_id"])
    assert [step.step_key for step in stored] == ["look", "charges"]
    assert all(step.step_type is WorkflowStepType.TOOL_CALL for step in stored)
    assert all(step.organization_id == account.organization_id for step in stored)
    assert all(step.started_at is not None and step.completed_at is not None for step in stored)


async def test_data_flows_from_the_input_into_a_tool(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    stored = await steps_of(session, run["run_id"])
    assert stored[0].output_data is not None
    assert stored[0].output_data["shipment"]["shipment_reference"] == REFERENCE


async def test_data_flows_from_one_step_into_the_next(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The second step reads a value the first produced, not the input."""
    account = await register(api_client)
    await seed(session, account.organization_id)

    workflow_id = await published(
        api_client,
        account.headers(),
        {
            "entry": "look",
            "steps": [
                tool_step(
                    "look",
                    "get_shipment",
                    arguments={"shipment_reference": "$.input.shipment_reference"},
                    next_step="again",
                ),
                tool_step(
                    "again",
                    "get_shipment_charges",
                    arguments={
                        "shipment_reference": ("$.steps.look.output.shipment.shipment_reference")
                    },
                    next_step=None,
                ),
            ],
        },
    )

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "succeeded"
    stored = await steps_of(session, run["run_id"])
    assert stored[1].output_data is not None
    assert stored[1].output_data["shipment_reference"] == REFERENCE


async def test_the_runs_output_is_the_last_steps_result(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    assert run["output"] is not None
    assert run["output"]["step"] == "charges"
    assert run["output"]["output"]["shipment_reference"] == REFERENCE


# -- Conditions ---------------------------------------------------------------


def branching(on_true: str | None, on_false: str | None) -> dict[str, object]:
    return {
        "entry": "look",
        "steps": [
            tool_step(
                "look",
                "get_shipment",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step="branch",
            ),
            condition_step(
                "branch",
                field="$.steps.look.output.shipment.status",
                operator="equals",
                value="exception",
                on_true=on_true,
                on_false=on_false,
            ),
            tool_step(
                "act",
                "get_shipment_charges",
                arguments={"shipment_reference": "$.input.shipment_reference"},
                next_step=None,
            ),
        ],
    }


async def test_a_condition_takes_the_true_branch(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.EXCEPTION)
    workflow_id = await published(api_client, account.headers(), branching("act", None))

    run = await start(api_client, account.headers(), workflow_id)

    assert [step["step_key"] for step in run["steps"]] == ["look", "branch", "act"]
    stored = await steps_of(session, run["run_id"])
    assert stored[1].output_data == {"result": True}


async def test_a_condition_takes_the_false_branch_and_can_end_the_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.IN_TRANSIT)
    workflow_id = await published(api_client, account.headers(), branching("act", None))

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "succeeded"
    assert [step["step_key"] for step in run["steps"]] == ["look", "branch"]
    stored = await steps_of(session, run["run_id"])
    assert stored[1].output_data == {"result": False}


async def test_the_branch_not_taken_leaves_no_step(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A step that did not run has no row, so nothing can read its output."""
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.IN_TRANSIT)
    workflow_id = await published(api_client, account.headers(), branching("act", None))

    run = await start(api_client, account.headers(), workflow_id)

    assert "act" not in {step["step_key"] for step in run["steps"]}


# -- Failures -----------------------------------------------------------------


async def test_a_tool_that_finds_nothing_stops_the_run_with_a_code(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A business failure is an answer about the process, not an HTTP error."""
    account = await register(api_client)
    # No shipment seeded, so the lookup finds nothing.
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "failed"
    assert run["error_code"] == "shipment_not_found"
    assert run["steps"][0]["status"] == "failed"
    assert len(run["steps"]) == 1, "the run stopped rather than carrying on"


async def test_a_failed_run_carries_no_tool_text_to_the_client(api_client: AsyncClient) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    payload = str(run)
    for forbidden in ("Traceback", "SELECT", "asyncpg", "psycopg", "password"):
        assert forbidden not in payload


async def test_a_reference_that_resolves_to_nothing_fails_the_step(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(
        api_client,
        account.headers(),
        {
            "entry": "look",
            "steps": [
                tool_step(
                    "look",
                    "get_shipment",
                    arguments={"shipment_reference": "$.input.absent"},
                    next_step=None,
                )
            ],
        },
    )

    run = await start(api_client, account.headers(), workflow_id, payload={"other": "x"})

    assert run["status"] == "failed"
    assert run["error_code"] == "workflow_reference_unresolved"


async def test_a_tool_called_with_the_wrong_arguments_fails_the_step(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The tool framework validates; the workflow records what it said."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(
        api_client,
        account.headers(),
        {
            "entry": "look",
            "steps": [tool_step("look", "get_shipment", arguments={"wrong": "x"}, next_step=None)],
        },
    )

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "failed"
    assert run["error_code"] == "tool_invalid_arguments"


async def test_an_output_larger_than_a_run_may_carry_is_refused(
    api_client: AsyncClient,
    session: AsyncSession,
    app,  # noqa: ANN001 - FastAPI
) -> None:
    """Refused rather than truncated: a shortened result is one a later
    condition would branch on without anybody knowing it was incomplete."""
    app.state.settings.workflow_max_output_bytes = 10

    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    assert run["status"] == "failed"
    assert run["error_code"] == "workflow_output_too_large"


# -- Tool executions ----------------------------------------------------------


async def test_every_tool_a_workflow_runs_is_recorded(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """One table records every tool the platform has run, whoever asked."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    executions = (
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.organization_id == account.organization_id
                )
            )
        )
        .scalars()
        .all()
    )

    assert {execution.tool_name for execution in executions} == {
        "get_shipment",
        "get_shipment_charges",
    }
    assert all(execution.run_id is None for execution in executions), "no agent run"
    assert all(execution.workflow_step_run_id is not None for execution in executions)
    assert all(execution.executed for execution in executions)
    steps = {step.id for step in await steps_of(session, run["run_id"])}
    assert {execution.workflow_step_run_id for execution in executions} <= steps


async def test_a_tool_execution_holds_no_arguments_or_records(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Metadata only. The payload is the workflow's context, which is a
    different table with a different purpose."""
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    await start(api_client, account.headers(), workflow_id)

    execution = (
        (
            await session.execute(
                select(ToolExecutionRecord).where(
                    ToolExecutionRecord.tool_name == "get_shipment",
                    ToolExecutionRecord.organization_id == account.organization_id,
                )
            )
        )
        .scalars()
        .one()
    )

    columns = {column.name for column in execution.__table__.c}
    assert "arguments" not in columns and "result" not in columns
    assert REFERENCE not in str({name: getattr(execution, name) for name in columns})
    assert execution.argument_count == 1


# -- Durability ---------------------------------------------------------------


async def test_a_run_survives_the_request_that_made_it(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    created = await start(api_client, account.headers(), workflow_id)

    fetched = await api_client.get(
        f"{WORKFLOWS_URL}/{workflow_id}/runs/{created['run_id']}", headers=account.headers()
    )

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "succeeded"
    assert fetched.json()["step_count"] == 2


async def test_the_steps_endpoint_reports_what_happened(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    created = await start(api_client, account.headers(), workflow_id)

    listed = await api_client.get(
        f"{WORKFLOWS_URL}/{workflow_id}/runs/{created['run_id']}/steps",
        headers=account.headers(),
    )

    assert [step["step_key"] for step in listed.json()] == ["look", "charges"]
    assert all("output" not in step for step in listed.json()), (
        "a step list says what happened, not what every tool read"
    )


async def test_a_run_records_which_version_it_followed(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    run = await start(api_client, account.headers(), workflow_id)

    assert run["workflow_version"] == 1
    record = await session.get(WorkflowRun, uuid.UUID(run["run_id"]))
    assert record is not None
    assert record.workflow_version == 1


async def test_a_step_cannot_be_recorded_twice(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The database refuses it, which is what makes duplicate execution
    impossible rather than unlikely."""
    from sqlalchemy.exc import IntegrityError

    account = await register(api_client)
    await seed(session, account.organization_id)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())
    run = await start(api_client, account.headers(), workflow_id)

    session.add(
        WorkflowStepRun(
            organization_id=account.organization_id,
            workflow_run_id=uuid.UUID(run["run_id"]),
            step_key="look",
            step_type=WorkflowStepType.TOOL_CALL,
            position=99,
            status=StepRunStatus.RUNNING,
        )
    )

    with pytest.raises(IntegrityError) as caught:
        await session.flush()

    assert "uq_workflow_step_runs_run_id_step_key" in str(caught.value)
    await session.rollback()


# -- The demonstration workflow ----------------------------------------------


async def test_the_demonstration_workflow_ends_early_for_a_healthy_shipment(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Nothing to do, so nothing is asked of anybody."""
    account = await register(api_client)
    await seed(session, account.organization_id, status=ShipmentStatus.IN_TRANSIT)
    workflow_id = await published(api_client, account.headers(), shipment_exception_definition())

    run = await start(
        api_client,
        account.headers(),
        workflow_id,
        payload={"shipment_reference": REFERENCE, "reason": REASON},
    )

    assert run["status"] == "succeeded"
    assert [step["step_key"] for step in run["steps"]] == [
        "get_shipment",
        "get_charges",
        "is_exception",
    ]
    assert run["approval"] is None


# -- Tenant isolation ---------------------------------------------------------


async def test_another_organization_cannot_read_a_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, ours.organization_id)
    workflow_id = await published(api_client, ours.headers(), lookup_workflow())
    run = await start(api_client, ours.headers(), workflow_id)

    response = await api_client.get(
        f"{WORKFLOWS_URL}/{workflow_id}/runs/{run['run_id']}", headers=theirs.headers()
    )

    assert response.status_code == 404


async def test_a_workflow_only_ever_sees_its_own_tenants_records(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Both organizations have a shipment with the same reference, and a run
    finds only its own."""
    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, ours.organization_id, status=ShipmentStatus.EXCEPTION)
    await seed(session, theirs.organization_id, status=ShipmentStatus.DELIVERED)

    workflow_id = await published(api_client, theirs.headers(), lookup_workflow())
    run = await start(api_client, theirs.headers(), workflow_id)

    stored = await steps_of(session, run["run_id"])
    assert stored[0].output_data is not None
    assert stored[0].output_data["shipment"]["status"] == "delivered"


async def test_a_step_cannot_be_attached_to_another_tenants_run(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Refused by the database: the composite key carries the organization."""
    from sqlalchemy.exc import IntegrityError

    ours = await register(api_client)
    theirs = await register(api_client)
    await seed(session, theirs.organization_id)
    workflow_id = await published(api_client, theirs.headers(), lookup_workflow())
    run = await start(api_client, theirs.headers(), workflow_id)

    session.add(
        WorkflowStepRun(
            organization_id=ours.organization_id,
            workflow_run_id=uuid.UUID(run["run_id"]),
            step_key="intruder",
            step_type=WorkflowStepType.TOOL_CALL,
            # A free position, so the composite foreign key is what refuses the
            # row rather than the run's own step ordering.
            position=99,
        )
    )

    with pytest.raises(IntegrityError) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


async def test_a_run_cannot_be_attached_to_another_tenants_workflow(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    from sqlalchemy.exc import IntegrityError

    ours = await register(api_client)
    theirs = await register(api_client)
    workflow_id = await published(api_client, theirs.headers(), lookup_workflow())

    session.add(
        WorkflowRun(
            organization_id=ours.organization_id,
            workflow_id=uuid.UUID(workflow_id),
            workflow_version=1,
            user_id=ours.user_id,
            status=RunStatus.PENDING,
        )
    )

    with pytest.raises(IntegrityError) as caught:
        await session.flush()

    assert "foreign key" in str(caught.value).lower()
    await session.rollback()


# -- Input security -----------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["organization_id", "user_id", "run_id", "request_id", "api_key", "password"]
)
async def test_input_cannot_name_identity_or_credentials(
    api_client: AsyncClient, field: str
) -> None:
    """Refused rather than ignored: silently dropping a field somebody sent is
    how they come to believe it worked."""
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs",
        json={"input": {field: "anything"}},
        headers=account.headers(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_invalid_input"


async def test_oversized_input_is_refused(api_client: AsyncClient) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs",
        json={"input": {"blob": "x" * 20_000}},
        headers=account.headers(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_invalid_input"


async def test_deeply_nested_input_is_refused(api_client: AsyncClient) -> None:
    account = await register(api_client)
    workflow_id = await published(api_client, account.headers(), lookup_workflow())

    nested: dict[str, object] = {"x": 1}
    for _ in range(20):
        nested = {"x": nested}

    response = await api_client.post(
        f"{WORKFLOWS_URL}/{workflow_id}/runs",
        json={"input": nested},
        headers=account.headers(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_invalid_input"
