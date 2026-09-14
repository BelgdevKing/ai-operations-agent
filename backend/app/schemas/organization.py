"""Organization and membership schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import MemberRole, MembershipStatus, OrganizationStatus


class OrganizationResponse(BaseModel):
    """An organization the caller belongs to."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrganizationStatus
    created_at: datetime


class MemberUser(BaseModel):
    """The person behind a membership.

    A deliberately thin view of ``users``: a colleague's name and address are
    what a member list needs, and nothing else from that table belongs here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    first_name: str | None
    last_name: str | None


class MemberResponse(BaseModel):
    """One membership of the current organization."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user: MemberUser
    role: MemberRole
    status: MembershipStatus
    created_at: datetime


class MemberRoleUpdate(BaseModel):
    """Change one member's role.

    The organization is never part of this payload: it comes from the caller's
    verified membership, so a request cannot name a tenant it does not belong
    to.
    """

    role: MemberRole = Field(description="owner, admin or member.")
