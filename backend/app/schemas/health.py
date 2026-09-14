"""Health endpoint schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: str = Field(description="Always 'ok' when the process is serving.")
    service: str
    version: str
    environment: str


class DependencyCheck(BaseModel):
    """Outcome for a single dependency.

    ``required`` says whether an unavailable dependency makes the service
    unready. Redis is optional until a feature actually needs it.
    """

    status: str = Field(description="'ok' or 'unavailable'.")
    required: bool


class ReadinessResponse(BaseModel):
    """Readiness payload: one entry per dependency, plus an overall verdict."""

    status: str = Field(description="'ready' or 'degraded'.")
    dependencies: dict[str, DependencyCheck]
