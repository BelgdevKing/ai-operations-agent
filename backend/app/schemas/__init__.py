"""Pydantic schemas - the contract at the API boundary.

ORM models never leave the service layer; endpoints accept and return these.
"""

from __future__ import annotations

from app.schemas.common import ErrorDetail, ErrorResponse, Page
from app.schemas.health import DependencyCheck, HealthResponse, ReadinessResponse

__all__ = [
    "DependencyCheck",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "Page",
    "ReadinessResponse",
]
