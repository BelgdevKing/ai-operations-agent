"""Password hashing and JWT handling. No database, no network."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import DEV_JWT_SECRET_KEY, Settings
from app.core.security import (
    ACCESS_TOKEN_TYPE,
    DUMMY_PASSWORD_HASH,
    TokenError,
    create_access_token,
    decode_access_token,
    needs_rehash,
    password_hash,
    verify_password,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings() -> Settings:
    """Reduced Argon2 cost so the suite is not dominated by hashing.

    Only the cost changes: the algorithm, salting and verification path under
    test are exactly those used in production.
    """
    return Settings(
        app_env="test",
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
    )


@pytest.fixture
def production_settings() -> Settings:
    """Real cost parameters, for the few tests that assert on them."""
    return Settings(app_env="test")


# -- Password hashing ---------------------------------------------------------


def test_hash_is_not_the_plaintext(settings: Settings) -> None:
    hashed = password_hash(PASSWORD, settings)

    assert hashed != PASSWORD
    assert PASSWORD not in hashed


def test_hash_is_argon2id(settings: Settings) -> None:
    """Argon2id specifically - argon2i and argon2d have different guarantees."""
    assert password_hash(PASSWORD, settings).startswith("$argon2id$")


def test_correct_password_verifies(settings: Settings) -> None:
    hashed = password_hash(PASSWORD, settings)

    assert verify_password(PASSWORD, hashed, settings) is True


def test_incorrect_password_fails(settings: Settings) -> None:
    hashed = password_hash(PASSWORD, settings)

    assert verify_password("not the password", hashed, settings) is False


@pytest.mark.parametrize(
    "wrong",
    [
        "",
        " ",
        "correct horse battery stapl",  # one character short
        "correct horse battery staple ",  # trailing space
        "CORRECT HORSE BATTERY STAPLE",  # case differs
    ],
)
def test_near_misses_fail(wrong: str, settings: Settings) -> None:
    hashed = password_hash(PASSWORD, settings)

    assert verify_password(wrong, hashed, settings) is False


def test_the_same_password_hashes_differently_each_time(settings: Settings) -> None:
    """Per-hash random salt. Without it, identical passwords would be visible
    as identical hashes and a rainbow table would work."""
    first = password_hash(PASSWORD, settings)
    second = password_hash(PASSWORD, settings)

    assert first != second
    assert verify_password(PASSWORD, first, settings)
    assert verify_password(PASSWORD, second, settings)


def test_verification_never_raises_on_a_corrupt_hash(settings: Settings) -> None:
    """A damaged column value must read as "wrong password", not as a 500."""
    for broken in ("", "not-a-hash", "$argon2id$garbage", "$2b$12$abcdefghijklmnop"):
        assert verify_password(PASSWORD, broken, settings) is False


def test_unicode_passwords_round_trip(settings: Settings) -> None:
    password = "pässwörd-日本語-🔐-ok"

    assert verify_password(password, password_hash(password, settings), settings) is True


def test_hash_fits_the_database_column(production_settings: Settings) -> None:
    """users.password_hash is VARCHAR(255); a silently truncated hash would
    lock the account out."""
    from app.models import User

    hashed = password_hash(PASSWORD, production_settings)
    limit = User.__table__.c.password_hash.type.length

    assert len(hashed) < limit


def test_dummy_hash_is_usable_and_matches_nothing() -> None:
    """The login path verifies against it when no user is found, so it has to
    be a real hash that no password satisfies."""
    settings = Settings(app_env="test")

    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert verify_password(PASSWORD, DUMMY_PASSWORD_HASH, settings) is False


def test_cost_parameters_reach_the_hash(settings: Settings) -> None:
    hashed = password_hash(PASSWORD, settings)

    assert "m=8192" in hashed
    assert "t=1" in hashed


def test_default_cost_meets_owasp_guidance(production_settings: Settings) -> None:
    """OWASP's floor for Argon2id is 19 MiB with 2 iterations."""
    assert production_settings.argon2_memory_cost_kib >= 19456
    assert production_settings.argon2_time_cost >= 2


def test_raised_cost_marks_old_hashes_for_rehash(settings: Settings) -> None:
    """Lets a cost increase apply on next login instead of a password reset."""
    old = password_hash(PASSWORD, settings)
    stronger = settings.model_copy(update={"argon2_memory_cost_kib": 16384})

    assert needs_rehash(old, settings) is False
    assert needs_rehash(old, stronger) is True


# -- JWT ----------------------------------------------------------------------


def test_a_fresh_token_decodes_to_its_subject(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, settings)

    claims = decode_access_token(token, settings)

    assert claims.user_id == user_id
    assert expires_in == settings.access_token_expire_minutes * 60


def test_token_carries_no_personal_data(settings: Settings) -> None:
    """Only an identifier. A token is not encrypted - anyone holding it can
    read the payload."""
    token, _ = create_access_token(uuid.uuid4(), settings)
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])

    assert set(payload) == {"sub", "iat", "exp", "type", "jti"}
    assert payload["type"] == ACCESS_TOKEN_TYPE


def test_tokens_are_unique_per_issue(settings: Settings) -> None:
    """The jti gives a future revocation list something to name."""
    user_id = uuid.uuid4()
    first, _ = create_access_token(user_id, settings)
    second, _ = create_access_token(user_id, settings)

    assert first != second


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    issued = datetime.now(UTC) - timedelta(minutes=settings.access_token_expire_minutes + 1)
    token, _ = create_access_token(uuid.uuid4(), settings, now=issued)

    with pytest.raises(TokenError):
        decode_access_token(token, settings)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    """The whole point of the signature: a forged token must not verify."""
    other_key = "an-entirely-different-secret-" + "x" * 16
    attacker = settings.model_copy(update={"jwt_secret_key": other_key})
    token, _ = create_access_token(uuid.uuid4(), attacker)

    with pytest.raises(TokenError):
        decode_access_token(token, settings)


def test_a_tampered_payload_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), settings)
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload[:-4]}AAAA.{signature}"

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


@pytest.mark.parametrize(
    "malformed",
    ["", "not a token", "a.b", "a.b.c", "...", "Bearer abc.def.ghi", "null"],
)
def test_malformed_tokens_are_rejected(malformed: str, settings: Settings) -> None:
    with pytest.raises(TokenError):
        decode_access_token(malformed, settings)


def test_an_unsigned_token_is_rejected(settings: Settings) -> None:
    """The alg=none attack. Rejected because decoding names the algorithm
    rather than trusting the token's own header."""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "type": ACCESS_TOKEN_TYPE,
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


def test_a_token_of_another_type_is_rejected(settings: Settings) -> None:
    """Stops a future refresh token being presented as an access token."""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "type": "refresh",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


def test_a_token_without_a_subject_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "type": ACCESS_TOKEN_TYPE,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


def test_a_non_uuid_subject_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "sub": "'; DROP TABLE users; --",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "type": ACCESS_TOKEN_TYPE,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


# -- Secret configuration -----------------------------------------------------


def test_development_runs_on_the_shipped_secret() -> None:
    assert Settings(app_env="development").uses_development_jwt_secret is True


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_deployed_environments_refuse_the_shipped_secret(environment: str) -> None:
    """The secret is in the repository, so anyone could forge tokens with it.
    Refusing to start is the only reliable way to prevent that reaching a
    deployment."""
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(app_env=environment, jwt_secret_key=DEV_JWT_SECRET_KEY)


def test_deployed_environments_refuse_a_short_secret() -> None:
    with pytest.raises(ValueError, match="at least"):
        Settings(app_env="production", jwt_secret_key="too-short")


def test_a_real_secret_is_accepted_in_production() -> None:
    settings = Settings(
        app_env="production",
        jwt_secret_key="x" * 48,
        database_url="postgresql+asyncpg://aiops:real@db:5432/aiops",
        debug=False,
        anthropic_api_key="test-anthropic-secret",
    )

    assert settings.uses_development_jwt_secret is False


def test_only_hmac_algorithms_are_configurable() -> None:
    """A public-key algorithm here would let the verification key sign."""
    from pydantic import ValidationError

    for algorithm in ("none", "RS256", "ES256"):
        with pytest.raises(ValidationError):
            Settings(jwt_algorithm=algorithm)
