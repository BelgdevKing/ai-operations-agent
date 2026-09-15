"""Version 1 API router.

Aggregates every v1 endpoint module. Mounted by the application factory under
``Settings.api_v1_prefix``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    agents,
    ai,
    approvals,
    auth,
    conversations,
    organizations,
    workflows,
)

router = APIRouter()

router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(ai.router, prefix="/ai", tags=["ai"])

# Under /ai rather than a second AI API: agents are the same capability
# with configuration and a loop around it, and share its conventions.
router.include_router(agents.router, prefix="/ai", tags=["agents"])

# Stored conversations belong to the same surface: they are what a run wrote.
router.include_router(conversations.router, prefix="/ai", tags=["conversations"])

# Workflows orchestrate the same capability, so they share its prefix rather
# than starting a third AI API.
router.include_router(workflows.router, prefix="/ai/workflows", tags=["workflows"])

# Approvals are their own resource rather than part of /ai. They are read and
# decided by people, not by agents, and an approval queue outlives the run that
# filled it - so it gets a top-level path rather than being nested under one.
router.include_router(approvals.router, prefix="/approvals", tags=["approvals"])

# Singular on purpose: a request acts on exactly one organization - the
# caller's - so there is no collection to address and no id in the path.
router.include_router(organizations.router, prefix="/organization", tags=["organization"])
