"""The run lifecycle, including the state that pauses it.

No database and no model: this is the state machine on its own, because that is
where an illegal transition has to be caught. Every move the durable layer makes
goes through one of these methods, so a run that could be completed twice - or
resumed after it failed - would make the ``agent_runs`` row untrustworthy no
matter how carefully the service layer were written.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.decisions import FinalDecision
from app.agents.exceptions import AgentStateError
from app.agents.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AgentRun,
    AgentRunCarryover,
    AgentRunStatus,
    AgentStep,
)
from app.ai.models import LLMUsage


def a_run(**overrides: object) -> AgentRun:
    return AgentRun(
        agent_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        **overrides,  # type: ignore[arg-type]
    )


def a_step(number: int = 1) -> AgentStep:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return AgentStep(
        number=number,
        decision=FinalDecision(content="done"),
        model="test-model",
        usage=LLMUsage(input_tokens=3, output_tokens=2),
        latency_ms=1.0,
        started_at=now,
        completed_at=now,
        message_count=2,
    )


# -- The legal moves ----------------------------------------------------------


def test_pending_to_running() -> None:
    run = a_run()
    assert run.status is AgentRunStatus.PENDING

    run.start()

    assert run.status is AgentRunStatus.RUNNING
    assert run.started_at is not None


def test_running_to_awaiting_approval() -> None:
    run = a_run()
    run.start()
    execution = uuid.uuid4()

    run.await_approval(tool_execution_id=execution)

    assert run.status is AgentRunStatus.AWAITING_APPROVAL
    assert run.pending_tool_execution_id == execution
    assert not run.is_terminal, "a paused run has not finished"
    assert run.completed_at is None


def test_awaiting_approval_to_running() -> None:
    """Resuming is the whole reason the state exists."""
    run = a_run()
    run.start()
    run.await_approval(tool_execution_id=uuid.uuid4())

    run.resume()

    assert run.status is AgentRunStatus.RUNNING
    assert run.pending_tool_execution_id is None, "nothing is waiting any more"


def test_awaiting_approval_to_cancelled() -> None:
    run = a_run()
    run.start()
    run.await_approval(tool_execution_id=uuid.uuid4())

    run.cancel()

    assert run.status is AgentRunStatus.CANCELLED
    assert run.final_response is None


def test_awaiting_approval_to_failed() -> None:
    """A resume can go wrong; the run must be able to say so."""
    run = a_run()
    run.start()
    run.await_approval(tool_execution_id=uuid.uuid4())

    run.fail(code="agent_run_error", message="The agent run failed.")

    assert run.status is AgentRunStatus.FAILED
    assert run.error_code == "agent_run_error"


def test_running_to_completed() -> None:
    run = a_run()
    run.start()

    run.complete(final_response="Here you are.")

    assert run.status is AgentRunStatus.COMPLETED
    assert run.final_response == "Here you are."
    assert run.completed_at is not None


def test_running_to_failed() -> None:
    run = a_run()
    run.start()

    run.fail(code="agent_max_steps_exceeded", message="It did not finish.")

    assert run.status is AgentRunStatus.FAILED
    assert run.completed_at is not None


def test_running_to_cancelled() -> None:
    run = a_run()
    run.start()

    run.cancel()

    assert run.status is AgentRunStatus.CANCELLED


# -- The illegal ones ---------------------------------------------------------


def test_a_paused_run_cannot_be_completed_directly() -> None:
    """Which is the point: a pause is not an answer.

    Representing a run waiting on a person as ``completed`` would be a lie in the
    one record an operator is most likely to trust.
    """
    run = a_run()
    run.start()
    run.await_approval(tool_execution_id=uuid.uuid4())

    with pytest.raises(AgentStateError):
        run.complete(final_response="Pretending it is done.")


def test_a_run_cannot_be_resumed_before_it_pauses() -> None:
    run = a_run()
    run.start()

    with pytest.raises(AgentStateError):
        run.resume()


def test_a_completed_run_cannot_be_resumed() -> None:
    run = a_run()
    run.start()
    run.complete(final_response="Done.")

    with pytest.raises(AgentStateError):
        run.resume()


def test_a_failed_run_cannot_be_resumed() -> None:
    """The abandonment sweep marks runs failed. Nothing may pick one back up."""
    run = a_run()
    run.start()
    run.fail(code="agent_run_abandoned", message="It stopped.")

    with pytest.raises(AgentStateError):
        run.resume()


def test_a_cancelled_run_cannot_be_resumed() -> None:
    run = a_run()
    run.start()
    run.cancel()

    with pytest.raises(AgentStateError):
        run.resume()


def test_a_completed_run_cannot_be_completed_again() -> None:
    run = a_run()
    run.start()
    run.complete(final_response="Done.")

    with pytest.raises(AgentStateError):
        run.complete(final_response="Done differently.")


def test_a_run_cannot_pause_before_it_starts() -> None:
    run = a_run()

    with pytest.raises(AgentStateError):
        run.await_approval(tool_execution_id=uuid.uuid4())


def test_a_paused_run_records_no_steps() -> None:
    """A step belongs to a run that is running. Nothing else."""
    run = a_run()
    run.start()
    run.await_approval(tool_execution_id=uuid.uuid4())

    with pytest.raises(AgentStateError):
        run.record(a_step())


def test_terminal_states_are_exactly_the_three_endings() -> None:
    assert TERMINAL_STATUSES == {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }
    assert AgentRunStatus.AWAITING_APPROVAL not in TERMINAL_STATUSES


def test_only_unfinished_running_states_are_sweepable() -> None:
    """What the abandonment sweep is allowed to consider.

    ``awaiting_approval`` being absent is the property that matters: an approval
    nobody has answered yet is old, and being old is not evidence of anything.
    """
    assert ACTIVE_STATUSES == {AgentRunStatus.PENDING, AgentRunStatus.RUNNING}
    assert AgentRunStatus.AWAITING_APPROVAL not in ACTIVE_STATUSES


# -- Carrying work across a pause ---------------------------------------------


def test_a_resumed_run_continues_its_budget_rather_than_restarting_it() -> None:
    """The step budget bounds the run, not one HTTP request.

    Without this a run could pause, resume, and spend its whole allowance again -
    which would turn "at most four model calls" into "four per approval".
    """
    run = a_run(
        status=AgentRunStatus.AWAITING_APPROVAL,
        carried=AgentRunCarryover(
            step_count=3,
            tool_call_count=1,
            input_tokens=100,
            output_tokens=40,
            latency_ms=250.0,
        ),
    )

    assert run.step_count == 3
    assert run.tool_call_count == 1
    assert run.usage.total_tokens == 140
    assert run.latency_ms == pytest.approx(250.0)

    run.resume()
    run.record(a_step(number=4))

    assert run.step_count == 4, "the fourth step, not the first"
    assert run.usage.total_tokens == 145


def test_a_fresh_run_carries_nothing() -> None:
    run = a_run()

    assert run.step_count == 0
    assert run.tool_call_count == 0
    assert run.usage.total_tokens == 0
    assert run.latency_ms == 0
