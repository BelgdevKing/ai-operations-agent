"""Business logic.

Services orchestrate repositories, enforce rules, and own the meaning of an
operation. They raise ``app.core.exceptions`` errors and never import FastAPI.
"""

from __future__ import annotations

from app.services.health import HealthService

__all__ = ["HealthService"]
