"""What a usage report looks like on the wire.

Counts, sums, model names, tool names and money. Nothing else, and there is no
field here that could carry anything else - no prompt, no answer, no tool
argument, no tool result, no conversation, no idempotency key, no credential.
The report is built from execution *records*, which never held any of those
either, so the absence is structural rather than filtered.

Money is serialised as a **string**. A JSON number becomes an IEEE double in
every client that reads it, and the point of carrying ``Decimal`` all the way
from the price book is lost at the last step if it is emitted as one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer

from app.observability.pricing import Cost
from app.repositories.usage import ModelUsage, StatusCounts
from app.services.usage import CostSummary, UsageReport

# Exact on the wire, and typed as a string in the schema so no client is
# tempted to parse it as a float.
Money = Annotated[Decimal, PlainSerializer(str, return_type=str, when_used="always")]


class TokenTotals(BaseModel):
    """Tokens, as the provider reported them."""

    input_tokens: int
    output_tokens: int
    total_tokens: int


class CostResponse(BaseModel):
    """What the priced part of this usage cost.

    ``unpriced_calls`` and ``unpriced_models`` are not decoration. A total that
    silently omitted the models it could not price would read as complete, and
    somebody would budget against it.
    """

    amount: Money = Field(description="Exact, as a string. Never a JSON number.")
    currency: str = Field(min_length=3, max_length=3)
    price_version: str = Field(description="Which price book produced this figure.")
    priced_calls: int
    unpriced_calls: int
    unpriced_models: list[str] = Field(
        description="Models used in this window that the deployment has no price for."
    )


class UnpricedResponse(BaseModel):
    """Usage that could not be priced at all.

    Sent instead of a cost - not a cost of zero. A deployment with no price book
    has complete usage and no idea what it is worth, and saying so is the only
    honest option.
    """

    amount: None = Field(default=None, description="Always null. Cost is unknown, not zero.")
    priced_calls: int = 0
    unpriced_calls: int
    unpriced_models: list[str]


class StatusBreakdown(BaseModel):
    """How many of something ended in each state."""

    total: int
    by_status: dict[str, int] = Field(
        description="Counts keyed by the lifecycle status. Keys are enum values."
    )

    @classmethod
    def from_counts(cls, counts: StatusCounts) -> StatusBreakdown:
        return cls(total=counts.total, by_status=dict(sorted(counts.by_status.items())))


class ModelUsageResponse(BaseModel):
    """One model's share of the window."""

    model: str
    calls: int
    tokens: TokenTotals
    cost: CostResponse | UnpricedResponse | None = Field(
        default=None, description="Null when the deployment has configured no prices at all."
    )


class ToolUsageResponse(BaseModel):
    """One tool's share of the window."""

    tool_name: str
    executions: int
    succeeded: int
    failed: int


class DailyUsageResponse(BaseModel):
    """One day's model calls."""

    day: datetime
    calls: int
    tokens: TokenTotals


class UsageResponse(BaseModel):
    """One organization's usage over one window."""

    organization_id: uuid.UUID = Field(
        description="Always the caller's own. The report is scoped before it is built."
    )
    since: datetime
    until: datetime = Field(description="Exclusive.")

    agent_runs: StatusBreakdown
    workflow_runs: StatusBreakdown
    tool_executions: StatusBreakdown
    approvals: StatusBreakdown

    llm_calls: int
    tokens: TokenTotals
    cost: CostResponse | UnpricedResponse

    models: list[ModelUsageResponse]
    tools: list[ToolUsageResponse]
    days: list[DailyUsageResponse] = Field(
        default_factory=list,
        description="Per-day breakdown. Empty unless the request asked for it.",
    )

    @classmethod
    def from_report(cls, report: UsageReport) -> UsageResponse:
        return cls(
            organization_id=report.organization_id,
            since=report.since,
            until=report.until,
            agent_runs=StatusBreakdown.from_counts(report.agent_runs),
            workflow_runs=StatusBreakdown.from_counts(report.workflow_runs),
            tool_executions=StatusBreakdown.from_counts(report.tool_executions),
            approvals=StatusBreakdown.from_counts(report.approvals),
            llm_calls=report.llm_calls,
            tokens=TokenTotals(
                input_tokens=report.input_tokens,
                output_tokens=report.output_tokens,
                total_tokens=report.total_tokens,
            ),
            cost=_cost(report.cost),
            models=[
                _model(usage, report.model_costs.get(usage.model), report.cost)
                for usage in report.models
            ],
            tools=[
                ToolUsageResponse(
                    tool_name=usage.tool_name,
                    executions=usage.executions,
                    succeeded=usage.succeeded,
                    failed=usage.failed,
                )
                for usage in report.tools
            ],
            days=[
                DailyUsageResponse(
                    day=usage.day,
                    calls=usage.calls,
                    tokens=TokenTotals(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.input_tokens + usage.output_tokens,
                    ),
                )
                for usage in report.days
            ],
        )


def _cost(summary: CostSummary) -> CostResponse | UnpricedResponse:
    """Money when there is any to report, and an explicit "unknown" otherwise."""
    if summary.cost is None:
        return UnpricedResponse(
            unpriced_calls=summary.unpriced_calls,
            unpriced_models=list(summary.unpriced_models),
        )

    return CostResponse(
        amount=summary.cost.amount,
        currency=summary.cost.currency,
        price_version=summary.cost.price_version,
        priced_calls=summary.priced_calls,
        unpriced_calls=summary.unpriced_calls,
        unpriced_models=list(summary.unpriced_models),
    )


def _model(usage: ModelUsage, cost: Cost | None, summary: CostSummary) -> ModelUsageResponse:
    """One model row, with what it cost if this deployment prices it.

    ``None`` for a deployment with no price book at all, and an explicit
    "unpriced" for a deployment that has one but not for this model. The two
    are different questions - "we do not do costs" and "we do, but not for
    this" - and a reader deserves to be able to tell them apart.
    """
    if cost is not None:
        priced: CostResponse | UnpricedResponse | None = CostResponse(
            amount=cost.amount,
            currency=cost.currency,
            price_version=cost.price_version,
            priced_calls=usage.calls,
            unpriced_calls=0,
            unpriced_models=[],
        )
    elif summary.cost is None and not summary.unpriced_models:
        priced = None
    else:
        priced = UnpricedResponse(unpriced_calls=usage.calls, unpriced_models=[usage.model])

    return ModelUsageResponse(
        model=usage.model,
        calls=usage.calls,
        tokens=TokenTotals(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        ),
        cost=priced,
    )
