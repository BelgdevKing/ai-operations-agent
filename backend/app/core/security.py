"""Password hashing and JSON Web Tokens.

Nothing cryptographic is implemented here. Hashing is ``argon2-cffi`` and
tokens are ``PyJWT``; this module supplies the parameters, the claim set, and
the validation rules around them.

Two properties matter and are easy to lose:

* Verification is constant-time with respect to the password. ``argon2-cffi``
  compares digests, never the inputs, so a wrong password costs the same as a
  right one.
* An unknown email costs the same as a known one. :func:`verify_password`
  against :data:`DUMMY_PASSWORD_HASH` gives the login path something to do when
  no user was found, so response time does not reveal which emails exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel

from app.core.config import Settings

# Distinguishes an access token from the refresh or verification tokens a later
# phase may add, so one can never be presented in place of another.
ACCESS_TOKEN_TYPE: Final = "access"


class TokenError(Exception):
    """A token was absent, malformed, expired, or not correctly signed.

    Deliberately one error for every cause: the API must not tell a caller
    which part of their token was wrong.
    """


class AccessTokenClaims(BaseModel):
    """The validated contents of an access token.

    Identity only. Roles and organization membership are read from the database
    on each request instead of being carried here - a token is valid until it
    expires, so a membership revoked or a role reduced mid-session would keep
    working if the token were the source of truth.
    """

    user_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime


def _hasher(settings: Settings) -> PasswordHasher:
    """Build the Argon2id hasher from configured cost parameters."""
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    )


def password_hash(password: str, settings: Settings) -> str:
    """Hash a password with Argon2id.

    The result embeds the algorithm, its parameters and a per-hash random salt,
    so two hashes of the same password never match and the cost can be raised
    later without invalidating existing hashes.
    """
    return _hasher(settings).hash(password)


def verify_password(password: str, hashed: str, settings: Settings) -> bool:
    """Return whether a password matches a hash, without raising.

    Every failure - wrong password, corrupt hash, unrecognised format - returns
    False. A caller that distinguished them would leak the difference.
    """
    try:
        return _hasher(settings).verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str, settings: Settings) -> bool:
    """Whether a stored hash predates the current cost parameters.

    Lets a raised cost be applied on next successful login rather than
    requiring a password reset.
    """
    try:
        return _hasher(settings).check_needs_rehash(hashed)
    except InvalidHashError:
        return True


# A real Argon2id hash of a throwaway value, used to spend the same time on a
# login for an address that does not exist as for one that does. Computed once
# at import with default parameters; it is never a valid credential because no
# user row ever carries it.
DUMMY_PASSWORD_HASH: Final = PasswordHasher().hash(uuid.uuid4().hex)


def create_access_token(
    user_id: uuid.UUID,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Sign an access token for a user.

    Returns the token and its lifetime in seconds, so a caller can report
    ``expires_in`` without decoding what it just created.
    """
    issued_at = now or datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    expires_at = issued_at + lifetime

    claims: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "type": ACCESS_TOKEN_TYPE,
        # Unique per token, so a future revocation list has something to name.
        "jti": uuid.uuid4().hex,
    }

    token = jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    """Verify a token's signature and expiry and return its claims.

    Raises :class:`TokenError` for every rejection.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            # A list of exactly the algorithm we issue. Without it PyJWT would
            # trust the token's own "alg" header, which is the algorithm
            # confusion attack.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat"], "verify_exp": True},
        )
    except jwt.PyJWTError as exc:
        raise TokenError("Could not validate credentials") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenError("Could not validate credentials")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (ValueError, KeyError) as exc:
        raise TokenError("Could not validate credentials") from exc

    return AccessTokenClaims(
        user_id=user_id,
        issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
