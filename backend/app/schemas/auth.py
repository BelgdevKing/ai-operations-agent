"""Authentication request and response schemas.

Responses are built from explicit fields rather than from the ORM object, so a
column added to ``users`` later - ``password_hash`` is already there - cannot
appear in a response by accident.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.enums import MemberRole, OrganizationStatus, UserStatus

# Upper bound guards the hasher: Argon2 cost grows with input, so an
# unbounded password is a cheap denial of service.
MAX_PASSWORD_LENGTH = 128

# Matches Settings.password_min_length. Declared here too so the limit appears
# in the OpenAPI schema; the service re-checks against configuration.
DEFAULT_MIN_PASSWORD_LENGTH = 12

Password = Annotated[
    str,
    Field(
        min_length=DEFAULT_MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description=(
            f"At least {DEFAULT_MIN_PASSWORD_LENGTH} characters. Length is the only "
            "rule: composition requirements encourage predictable substitutions "
            "without adding entropy."
        ),
    ),
]


def normalize_email(value: str) -> str:
    """Lowercase and trim an address.

    Addresses are compared and stored in this form so that ``Ada@Example.com``
    and ``ada@example.com`` cannot become two accounts. The local part is
    technically case-sensitive per RFC 5321, but no mainstream provider treats
    it that way, and allowing both would be an account-takeover footgun.
    """
    return value.strip().lower()


class RegisterRequest(BaseModel):
    """Create a user, their organization, and their owner membership."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: Password
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    organization_name: str = Field(
        min_length=1,
        max_length=200,
        description="Name of the organization created for this user, who becomes its owner.",
    )

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_email(value)

    @model_validator(mode="after")
    def _password_must_not_contain_the_email(self) -> Self:
        """A password built from the address is guessable from the address."""
        local_part = self.email.split("@", 1)[0]
        if len(local_part) >= 4 and local_part in self.password.lower():
            raise ValueError("Password must not contain your email address.")
        return self


class LoginRequest(BaseModel):
    """Exchange credentials for an access token."""

    model_config = ConfigDict(str_strip_whitespace=True)

    # Not EmailStr: rejecting a malformed address here would answer "does this
    # address exist?" differently from "is this password right?".
    email: str = Field(max_length=320)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_email(value)


class TokenResponse(BaseModel):
    """An issued access token."""

    access_token: str
    token_type: str = Field(default="bearer", description="Always 'bearer'.")
    expires_in: int = Field(description="Lifetime in seconds from issue.")


class OrganizationSummary(BaseModel):
    """An organization as seen from inside it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: OrganizationStatus


class MembershipSummary(BaseModel):
    """The caller's standing in one organization."""

    model_config = ConfigDict(from_attributes=True)

    organization: OrganizationSummary
    role: MemberRole


class UserProfile(BaseModel):
    """The authenticated user's own profile.

    Every field is listed explicitly. ``password_hash`` has no counterpart here
    and cannot be added by accident.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    first_name: str | None
    last_name: str | None
    status: UserStatus


class CurrentUserResponse(BaseModel):
    """Answer to ``GET /auth/me``: who the caller is and where they belong."""

    user: UserProfile
    memberships: list[MembershipSummary]


class RegisterResponse(BaseModel):
    """Result of registration, including the token to continue with."""

    user: UserProfile
    organization: OrganizationSummary
    role: MemberRole
    token: TokenResponse
