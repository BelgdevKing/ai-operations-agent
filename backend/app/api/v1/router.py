"""Version 1 API router.

Aggregates every v1 endpoint module. Mounted by the application factory under
``Settings.api_v1_prefix``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import ai, auth, organizations

router = APIRouter()

router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(ai.router, prefix="/ai", tags=["ai"])

# Singular on purpose: a request acts on exactly one organization - the
# caller's - so there is no collection to address and no id in the path.
router.include_router(organizations.router, prefix="/organization", tags=["organization"])
