"""What one organization used, and what it cost.

Composes two things that are deliberately kept apart everywhere else: the
*counts*, which come from the execution records a tenant already owns, and the
*prices*, which come from deployment configuration. Neither knows about the
other until here.

**Usage is not audit and not metrics.** The audit trail answers "what happened,
and who did it"; the metric registry answers "is the fleet healthy"; this
answers "how much did this organization do". They read different things and are
allowed to disagree about grain: an audit row per event, a metric per process, a
figure per tenant per window.

**Cost is honest about what it does not know.** A model with no configured price
contributes tokens and no money, and the report says how many calls were in that
position - so "cost: 12.40" is never quietly "cost of the part we could price".
A deployment with no price book at all reports complete usage and no cost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.models.organization import OrganizationMember
from app.observability.pricing import Cost, PriceBook
from app.repositories.usage import (
    DailyUsage,
    ModelUsage,
    StatusCounts,
    ToolUsage,
    UsageRepository,
)


class UsageWindowError(ValidationError):
    """The requested period is not one this API will aggregate."""

    code = "usage_window_invalid"


@dataclass(frozen=True)
class CostSummary:
    """Money, and how much of the usage it actually covers.

    ``unpriced_calls`` is the field that keeps the figure honest. A total with
    no denominator invites the reader to assume it is complete.
    """

    cost: Cost | None
    priced_calls: int
    unpriced_calls: int
    unpriced_models: tuple[str, ...]


@dataclass(frozen=True)
class UsageReport:
    """One organization's usage over one window."""

    organization_id: uuid.UUID
    since: datetime
    until: datetime

    agent_runs: StatusCounts
    workflow_runs: StatusCounts
    tool_executions: StatusCounts
    approvals: StatusCounts

    models: tuple[ModelUsage, ...]
    tools: tuple[ToolUsage, ...]
    days: tuple[DailyUsage, ...]

    cost: CostSummary
    #: Per model, so a caller can see where the money went without pricing
    #: anything a second time. ``None`` for a model this deployment has no
    #: price for - which is different from a cost of zero.
    model_costs: dict[str, Cost | None] = field(default_factory=dict)

    @property
    def llm_calls(self) -> int:
        return sum(model.calls for model in self.models)

    @property
    def input_tokens(self) -> int:
        return sum(model.input_tokens for model in self.models)

    @property
    def output_tokens(self) -> int:
        return sum(model.output_tokens for model in self.models)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageService:
    """Reports usage for one verified membership."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        membership: OrganizationMember,
    ) -> None:
        self._settings = settings

        # From the caller's verified membership, once, here. There is no
        # argument below that takes an organization, so no request can report
        # on a tenant other than its own.
        self._organization_id = membership.organization_id
        self._usage = UsageRepository(session, self._organization_id)

    @property
    def price_book(self) -> PriceBook:
        return self._settings.price_book

    async def report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_days: bool = False,
    ) -> UsageReport:
        """Everything this organization used between two instants.

        Args:
            since: Start of the window. Defaults to the configured span back
                from *until*.
            until: End, exclusive. Defaults to now.
            include_days: Whether to compute the per-day breakdown, which is a
                sixth query and is only worth paying for when somebody is
                drawing a chart.

        Raises:
            UsageWindowError: The window is inverted, in the future beyond a
                minute's clock skew, or longer than the deployment allows.
        """
        start, end = self._window(since, until)

        models = tuple(await self._usage.model_usage(since=start, until=end))
        tools = tuple(await self._usage.tool_usage(since=start, until=end))
        days = tuple(await self._usage.daily_usage(since=start, until=end)) if include_days else ()

        book = self.price_book
        summary = self.estimate(models, at=end)

        return UsageReport(
            organization_id=self._organization_id,
            since=start,
            until=end,
            agent_runs=await self._usage.agent_run_counts(since=start, until=end),
            workflow_runs=await self._usage.workflow_run_counts(since=start, until=end),
            tool_executions=await self._usage.tool_execution_counts(since=start, until=end),
            approvals=await self._usage.approval_counts(since=start, until=end),
            models=models,
            tools=tools,
            days=days,
            cost=summary,
            model_costs={
                usage.model: book.estimate(
                    usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    at=end,
                )
                for usage in models
            },
        )

    def estimate(self, models: tuple[ModelUsage, ...], *, at: datetime) -> CostSummary:
        """Price what can be priced, and say what could not.

        Each model is priced at the book in force at *at* - the end of the
        window - rather than per call. Pricing each historical call at the price
        in force when it ran would be more precise and would also mean a report
        whose total changes depending on when it is asked; a window priced at
        one instant is reproducible, which is what a bill needs to be.
        """
        book = self.price_book

        costs: list[Cost] = []
        priced = 0
        unpriced = 0
        unpriced_models: list[str] = []

        for usage in models:
            cost = book.estimate(
                usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                at=at,
            )
            if cost is None:
                unpriced += usage.calls
                unpriced_models.append(usage.model)
                continue
            costs.append(cost)
            priced += usage.calls

        return CostSummary(
            cost=book.total(costs),
            priced_calls=priced,
            unpriced_calls=unpriced,
            unpriced_models=tuple(sorted(unpriced_models)),
        )

    # -- The window ------------------------------------------------------------

    def _window(self, since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
        """Resolve and check the period, refusing anything unbounded.

        The ceiling is the point: these queries are aggregates over indexes on
        ``(organization_id, created_at)``, and a window nobody bounded is the
        one shape that would walk a tenant's whole history.
        """
        now = datetime.now(UTC)
        span = timedelta(days=self._settings.usage_window_days)

        end = _aware(until) if until else now
        start = _aware(since) if since else end - span

        if start >= end:
            raise UsageWindowError("The start of a usage window must come before its end.")

        # A minute of slack, because a client's clock is not this server's and
        # "now" rounded up by a few seconds should not be an error.
        if start > now + timedelta(minutes=1):
            raise UsageWindowError("A usage window cannot start in the future.")

        if end - start > span:
            raise UsageWindowError(
                f"A usage window may cover at most {self._settings.usage_window_days} days."
            )

        return start, end


def _aware(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than rejecting it.

    Every timestamp this platform stores is timezone-aware; a naive one can
    only have come from a client that dropped the offset, and assuming UTC is
    both what it meant and what the database would have done.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
