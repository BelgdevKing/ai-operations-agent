"""Shared response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """The error itself, separated from the envelope around it."""

    code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable description.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured context, such as which fields failed validation.",
    )


class ErrorResponse(BaseModel):
    """Every error response the API returns has this shape."""

    error: ErrorDetail
    request_id: str | None = Field(
        default=None,
        description="Correlation id for this request, matching the X-Request-ID header.",
    )


class Page[ItemT](BaseModel):
    """A page of results.

    Defined now so that listing endpoints are consistent from the first one.
    """

    items: list[ItemT]
    total: int = Field(ge=0, description="Total matching records, ignoring pagination.")
    limit: int = Field(ge=1, description="Maximum records requested.")
    offset: int = Field(ge=0, description="Records skipped before this page.")

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
