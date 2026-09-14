"""Version 1 API router.

Aggregates every v1 endpoint module. Mounted by the application factory under
``Settings.api_v1_prefix``.

No resources are exposed yet - tenants are the first, in the multi-tenancy
phase. Endpoint modules are registered here as they arrive:

    from app.api.v1.endpoints import tenants
    router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
