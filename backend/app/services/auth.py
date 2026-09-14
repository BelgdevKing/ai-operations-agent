"""Registration and login.

No FastAPI here. The service raises ``app.core.exceptions`` errors and the API
layer turns them into responses, which is what lets the same logic be called
later from a worker or a CLI.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, UnauthorizedError, ValidationError
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    needs_rehash,
    password_hash,
    verify_password,
)
from app.models.enums import MemberRole, MembershipStatus, OrganizationStatus, UserStatus
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.repositories.organization import OrganizationRepository
from app.repositories.user import UserRepository
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

logger = logging.getLogger(__name__)

MAX_SLUG_LENGTH = 100
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Turn an organization name into a URL-safe slug.

    Accents are folded rather than dropped, so "Café Ltd" becomes "cafe-ltd"
    instead of "caf-ltd".
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", folded.lower()).strip("-")[:MAX_SLUG_LENGTH]
    return slug or "org"


class AuthService:
    """Creates accounts and exchanges credentials for tokens."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.users = UserRepository(session)
        self.organizations = OrganizationRepository(session)

    # -- Registration ---------------------------------------------------------

    async def register(self, request: RegisterRequest) -> tuple[User, Organization, MemberRole]:
        """Create a user, an organization, and an owner membership joining them.

        One unit of work: the caller's session commits all three or none, so a
        failure cannot leave a user with no organization to belong to.
        """
        self._validate_password(request.password)

        if await self.users.email_exists(request.email):
            # Registration cannot hide this - the address either becomes an
            # account or it does not. Login is where the distinction matters,
            # and there it is not revealed.
            raise ConflictError("An account with that email already exists.")

        user = User(
            email=request.email,
            password_hash=password_hash(request.password, self.settings),
            first_name=request.first_name,
            last_name=request.last_name,
            # Self-registration with no email verification step yet; a
            # verification phase would start this at PENDING instead.
            status=UserStatus.ACTIVE,
        )
        self.users.add(user)

        organization = Organization(
            name=request.organization_name,
            slug=await self._unique_slug(request.organization_name),
            status=OrganizationStatus.ACTIVE,
        )
        self.organizations.add(organization)
        await self.session.flush()

        membership = OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            # Whoever creates the organization owns it. Without this there
            # would be nobody able to administer it.
            role=MemberRole.OWNER,
            status=MembershipStatus.ACTIVE,
        )
        self.session.add(membership)
        await self.session.flush()

        logger.info(
            "Registered user",
            extra={"context": {"user_id": str(user.id), "organization_id": str(organization.id)}},
        )
        return user, organization, MemberRole.OWNER

    # -- Login ----------------------------------------------------------------

    async def authenticate(self, request: LoginRequest) -> User:
        """Verify credentials and return the user.

        Every failure raises the same error with the same message. The work
        done is also the same: when no user matches, the password is verified
        against a dummy hash so that a missing address and a wrong password
        take the same time.
        """
        user = await self.users.get_by_email(request.email)

        if user is None or user.password_hash is None:
            verify_password(request.password, DUMMY_PASSWORD_HASH, self.settings)
            raise self._invalid_credentials()

        if not verify_password(request.password, user.password_hash, self.settings):
            raise self._invalid_credentials()

        if user.status is not UserStatus.ACTIVE:
            # Same message again: that an address belongs to a suspended
            # account is information a stranger should not be able to confirm.
            raise self._invalid_credentials()

        # A raised Argon2 cost applies on next login rather than needing a
        # password reset.
        if needs_rehash(user.password_hash, self.settings):
            user.password_hash = password_hash(request.password, self.settings)

        return user

    def issue_token(self, user_id: uuid.UUID) -> TokenResponse:
        """Sign an access token for an authenticated user."""
        token, expires_in = create_access_token(user_id, self.settings)
        return TokenResponse(access_token=token, expires_in=expires_in)

    # -- Internals ------------------------------------------------------------

    def _invalid_credentials(self) -> UnauthorizedError:
        """One message for every login failure.

        Nothing is logged about which address was tried: failed logins are
        attacker-controlled input, and recording them invites both log
        injection and a plaintext password ending up in a log line through a
        mistyped field.
        """
        return UnauthorizedError("Incorrect email or password.")

    def _validate_password(self, password: str) -> None:
        """Re-check length against configuration.

        The schema already enforces a static bound so the limit appears in
        OpenAPI; this enforces the configured one, which an operator can raise.
        """
        if len(password) < self.settings.password_min_length:
            raise ValidationError(
                f"Password must be at least {self.settings.password_min_length} characters."
            )

    async def _unique_slug(self, name: str) -> str:
        """Derive a slug, appending a suffix until it is free.

        A unique index backs this up: the loop avoids the common collision,
        the constraint catches the race.
        """
        base = slugify(name)
        if not await self.organizations.slug_exists(base):
            return base

        for _ in range(5):
            suffix = uuid.uuid4().hex[:6]
            candidate = f"{base[: MAX_SLUG_LENGTH - len(suffix) - 1]}-{suffix}"
            if not await self.organizations.slug_exists(candidate):
                return candidate

        raise ConflictError("Could not allocate an organization slug; try a different name.")
