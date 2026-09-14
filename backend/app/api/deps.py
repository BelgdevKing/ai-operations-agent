"""Shared FastAPI dependencies.

Annotated aliases keep endpoint signatures short and make the wiring explicit
in one place instead of being repeated across routers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.services.health import HealthService


def get_app_settings(request: Request) -> Settings:
    """Return the settings the running application was built with.

    Read from application state rather than the process-wide loader, so an
    application constructed with explicit settings - as tests do - behaves
    consistently everywhere, including inside endpoints.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
"""Settings of the running application."""

SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""Database session scoped to the request; commits on success, rolls back on error."""


def get_health_service(settings: SettingsDep) -> HealthService:
    return HealthService(settings)


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
"""Health and readiness reporting."""
