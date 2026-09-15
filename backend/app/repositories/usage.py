"""Reading usage back out of the execution records.

**No new table, and that is the design.** Every figure a usage report needs is
already durable, at the right grain, in the tables Parts 16-18 wrote:

    agent_steps        one row per model call inside a run
    generation_usage   one row per direct /ai/generate call
    agent_runs         one row per run: status, step and tool counts, tokens
    tool_executions    one row per execution: tool, outcome, duration
    workflow_runs      one row per workflow run: status
    approvals          one row per request for a person: status, timestamps

**Model calls come from two tables and are added, never counted twice.** The two
are disjoint by construction: an agent run writes ``agent_steps`` and never
``generation_usage``, and ``/ai/generate`` writes ``generation_usage`` and
creates no run at all. So the union of them is the set of model calls, and
:meth:`UsageRepository._calls` is the one place that union is written - every
token figure in this file reads it rather than either table directly.

and each of them already carries an ``(organization_id, created_at)`` index,
because each of them was already read that way. A ``usage_records`` table would
be a second copy of all of it, written on the same paths, in the same
transactions - and a second copy that can disagree with the first about how many
tokens a run used is worse than a query that takes a few milliseconds.

**Tenancy.** This class spans five models, so it cannot be a
``TenantScopedRepository`` - that class binds one. The guarantee is preserved a
different way and is still checkable by reading one method: every statement in
this file is built by :meth:`_scoped`, which is the only place the organization
filter is written, and the organization comes from the caller's verified
membership. A query that did not go through it would be visible in a diff.

**Bounded.** Every query is an aggregate - ``COUNT``, ``SUM``, ``GROUP BY`` -
over a window the API caps. Nothing here returns a row of business data, and
there is no method that could: the columns selected are counts, sums and
enumerated statuses.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Subquery

from app.models.agent_run import AgentRunRecord, AgentStepRecord, ToolExecutionRecord
from app.models.approval import Approval
from app.models.base import Base
from app.models.generation import GenerationUsage
from app.models.workflow import WorkflowRun

# How many distinct groups one report may return. A report is a summary; a
# tenant with more models or tools than this has an export problem, which is a
# different feature.
MAX_GROUPS = 200


@dataclass(frozen=True)
class ModelUsage:
    """What one model was asked to do, in one window."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ToolUsage:
    """What one tool did, in one window."""

    tool_name: str
    executions: int
    succeeded: int
    failed: int


@dataclass(frozen=True)
class DailyUsage:
    """One day's model calls."""

    day: datetime
    calls: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class StatusCounts:
    """How many of something ended in each state."""

    by_status: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.by_status.values())

    def of(self, *statuses: str) -> int:
        return sum(self.by_status.get(status, 0) for status in statuses)


class UsageRepository:
    """Aggregates one organization's execution records."""

    def __init__(self, session: AsyncSession, organization_id: uuid.UUID) -> None:
        self.session = session
        self.organization_id = organization_id

    # -- The one place the filter is written -----------------------------------

    def _scoped(
        self,
        model: type[Base],
        # Any, because the columns are aggregates - Label[int], a func
        # expression, an InstrumentedAttribute - and SQLAlchemy has no one
        # type that covers them. The safety here is not in this signature:
        # it is that this is the only method that builds a statement.
        *columns: Any,  # noqa: ANN401
        since: datetime,
        until: datetime,
    ) -> Select[Any]:
        """A statement over *model*, confined to this tenant and this window.

        Every query below starts here. The organization predicate and the time
        bounds are applied together because a report is always both - an
        unbounded window over one tenant is the query shape that would not use
        the ``(organization_id, created_at)`` index these tables already carry.
        """
        created = model.__table__.c["created_at"]
        return select(*columns).where(
            model.__table__.c["organization_id"] == self.organization_id,
            created >= since,
            created < until,
        )

    # -- Model calls -----------------------------------------------------------

    def _calls(self, *, since: datetime, until: datetime) -> Subquery:
        """Every model call this organization made in the window.

        The union of the two places a call is recorded, and the only place that
        union is written. They cannot overlap: an agent run writes a step and no
        generation row, and a direct generation writes a generation row and no
        run - so ``UNION ALL`` adds them rather than double-counting, and
        ``UNION`` (which would deduplicate) would be the wrong operator, since
        two identical calls are two calls.

        Each half goes through :meth:`_scoped`, so the tenant filter and the
        window are applied to both by the same code.
        """
        steps = self._scoped(
            AgentStepRecord,
            AgentStepRecord.model.label("model"),
            AgentStepRecord.input_tokens.label("input_tokens"),
            AgentStepRecord.output_tokens.label("output_tokens"),
            AgentStepRecord.__table__.c["created_at"].label("occurred_at"),
            since=since,
            until=until,
        )
        direct = self._scoped(
            GenerationUsage,
            GenerationUsage.model.label("model"),
            GenerationUsage.input_tokens.label("input_tokens"),
            GenerationUsage.output_tokens.label("output_tokens"),
            GenerationUsage.__table__.c["created_at"].label("occurred_at"),
            since=since,
            until=until,
        )
        return steps.union_all(direct).subquery("model_calls")

    async def model_usage(self, *, since: datetime, until: datetime) -> Sequence[ModelUsage]:
        """Calls and tokens per model, across both sources.

        A sum over facts rather than an estimate. ``agent_steps`` is one row per
        model call with ``UNIQUE(run_id, step_number)``, so a replayed step is a
        collision rather than a second row and a run resumed after an approval
        contributes each of its calls once; ``generation_usage`` is written once
        per direct call, after the provider has answered.
        """
        calls = self._calls(since=since, until=until)

        statement = (
            select(
                calls.c.model,
                func.count().label("calls"),
                func.coalesce(func.sum(calls.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(calls.c.output_tokens), 0).label("output_tokens"),
            )
            .group_by(calls.c.model)
            .order_by(func.count().desc(), calls.c.model)
            .limit(MAX_GROUPS)
        )

        rows = (await self.session.execute(statement)).all()
        return [
            ModelUsage(
                model=row.model,
                calls=int(row.calls),
                input_tokens=int(row.input_tokens),
                output_tokens=int(row.output_tokens),
            )
            for row in rows
        ]

    async def daily_usage(self, *, since: datetime, until: datetime) -> Sequence[DailyUsage]:
        """Calls and tokens per day, across both sources, in one query."""
        calls = self._calls(since=since, until=until)
        day = func.date_trunc("day", calls.c.occurred_at).label("day")

        statement = (
            select(
                day,
                func.count().label("calls"),
                func.coalesce(func.sum(calls.c.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(calls.c.output_tokens), 0).label("output_tokens"),
            )
            .group_by(day)
            .order_by(day)
            .limit(MAX_GROUPS)
        )

        rows = (await self.session.execute(statement)).all()
        return [
            DailyUsage(
                day=row.day,
                calls=int(row.calls),
                input_tokens=int(row.input_tokens),
                output_tokens=int(row.output_tokens),
            )
            for row in rows
        ]

    # -- Tools -----------------------------------------------------------------

    async def tool_usage(self, *, since: datetime, until: datetime) -> Sequence[ToolUsage]:
        """Executions per tool, split by whether they worked.

        The tool *name* is a column on the execution record, so this needs no
        join and discloses nothing beyond which capabilities were used - never
        what they were asked or what they returned, neither of which is stored
        on an execution row at all.
        """
        succeeded = func.count().filter(ToolExecutionRecord.outcome == "succeeded")
        failed = func.count().filter(ToolExecutionRecord.outcome.in_(("failed", "timed_out")))

        statement = (
            self._scoped(
                ToolExecutionRecord,
                ToolExecutionRecord.tool_name,
                func.count().label("executions"),
                succeeded.label("succeeded"),
                failed.label("failed"),
                since=since,
                until=until,
            )
            .group_by(ToolExecutionRecord.tool_name)
            .order_by(func.count().desc(), ToolExecutionRecord.tool_name)
            .limit(MAX_GROUPS)
        )

        rows = (await self.session.execute(statement)).all()
        return [
            ToolUsage(
                tool_name=row.tool_name,
                executions=int(row.executions),
                succeeded=int(row.succeeded),
                failed=int(row.failed),
            )
            for row in rows
        ]

    # -- Lifecycles ------------------------------------------------------------

    async def agent_run_counts(self, *, since: datetime, until: datetime) -> StatusCounts:
        return await self._counts_by_status(AgentRunRecord, since=since, until=until)

    async def workflow_run_counts(self, *, since: datetime, until: datetime) -> StatusCounts:
        return await self._counts_by_status(WorkflowRun, since=since, until=until)

    async def approval_counts(self, *, since: datetime, until: datetime) -> StatusCounts:
        return await self._counts_by_status(Approval, since=since, until=until)

    async def tool_execution_counts(self, *, since: datetime, until: datetime) -> StatusCounts:
        """Executions by outcome.

        ``outcome`` is nullable - it is null while an execution is waiting on a
        person - so the null bucket is named in Python rather than with a
        ``COALESCE``. PostgreSQL will not group by an expression built from a
        bind parameter, and writing the default into the SQL as a literal to get
        around that is exactly the habit that ends in an injection.
        """
        column = ToolExecutionRecord.__table__.c["outcome"]
        statement = self._scoped(
            ToolExecutionRecord,
            column.label("status"),
            func.count().label("rows"),
            since=since,
            until=until,
        ).group_by(column)

        rows = (await self.session.execute(statement)).all()
        return StatusCounts(
            {(row.status if row.status is not None else "pending"): int(row.rows) for row in rows}
        )

    async def _counts_by_status(
        self, model: type[Base], *, since: datetime, until: datetime
    ) -> StatusCounts:
        column = model.__table__.c["status"]
        statement = self._scoped(
            model, column.label("status"), func.count().label("rows"), since=since, until=until
        ).group_by(column)

        rows = (await self.session.execute(statement)).all()
        return StatusCounts({str(row.status): int(row.rows) for row in rows})
