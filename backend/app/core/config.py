"""Application settings, loaded from environment variables.

Every setting has a development-friendly default so the application, the test
suite and the tooling all start without a populated environment. Production
values are supplied by the environment; nothing secret is ever hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogFormat = Literal["json", "console"]

# Only HMAC algorithms are accepted. Restricting the set is what prevents an
# algorithm-confusion attack, where a token claims "alg": "none" or an
# asymmetric algorithm that turns the public key into a signing key.
JWTAlgorithm = Literal["HS256", "HS384", "HS512"]

# Deliberately obvious. Never use this outside local development.
DEV_JWT_SECRET_KEY = "dev-only-insecure-jwt-secret-change-me"

# Below this, a brute-forced HMAC key is within reach.
MIN_PRODUCTION_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------------
    app_name: str = "AI Operations Agent Platform"
    app_env: Environment = "development"
    debug: bool = True

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    # Mount point for the versioned API. Probe endpoints stay outside it.
    api_v1_prefix: str = "/api/v1"

    # -- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    # "console" is easier to read while developing; "json" is machine-readable.
    log_format: LogFormat = "console"

    # -- CORS ----------------------------------------------------------------
    # Comma-separated; parsed by cors_origin_list below.
    cors_origins: str = "http://localhost:3000"

    # -- PostgreSQL ----------------------------------------------------------
    # Defaults target a native localhost install; docker-compose.yml overrides
    # the hosts with its service names.
    database_url: str = "postgresql+asyncpg://aiops:aiops_dev_password@localhost:5432/aiops"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout: int = Field(default=30, ge=1)

    # -- Redis ---------------------------------------------------------------
    # Redis is not used by any implemented feature yet, so native development
    # does not need it running. Compose sets redis_required to true, where Redis
    # is part of the stack. When false, an unreachable Redis is reported but
    # does not make the service unready.
    redis_url: str = "redis://localhost:6379/0"
    redis_required: bool = False

    # -- Authentication ------------------------------------------------------
    # The development default below is public knowledge: it is in the
    # repository. Anything signed with it can be forged by anyone. Production
    # MUST set JWT_SECRET_KEY to a long random value, and the validator at the
    # bottom of this class refuses to start if it has not been.
    #
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: str = DEV_JWT_SECRET_KEY
    jwt_algorithm: JWTAlgorithm = "HS256"
    access_token_expire_minutes: int = Field(default=30, ge=1)

    # Length only. Composition rules (a digit, a symbol...) push people towards
    # predictable substitutions without adding real entropy, which is why
    # NIST SP 800-63B advises against them.
    password_min_length: int = Field(default=12, ge=8)

    # Argon2id hashing cost. Defaults are above the OWASP minimum of 19 MiB
    # with 2 iterations. Raise memory first: it is what makes GPU cracking
    # expensive.
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost_kib: int = Field(default=65536, ge=8192)
    argon2_parallelism: int = Field(default=4, ge=1)

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @model_validator(mode="after")
    def _require_a_real_secret_outside_development(self) -> Settings:
        """Refuse to start a deployed environment on the shipped secret.

        A warning would be missed. Failing at start-up means the mistake
        cannot reach production quietly - the application simply does not come
        up until JWT_SECRET_KEY is set.
        """
        if self.app_env in ("production", "staging"):
            if self.jwt_secret_key == DEV_JWT_SECRET_KEY:
                raise ValueError(
                    "JWT_SECRET_KEY is still the development default. Set it to a "
                    'random value: python -c "import secrets; '
                    'print(secrets.token_urlsafe(48))"'
                )
            if len(self.jwt_secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {MIN_PRODUCTION_SECRET_LENGTH} "
                    f"characters in {self.app_env}."
                )
        return self

    @property
    def uses_development_jwt_secret(self) -> bool:
        """True when tokens are signed with the public development key."""
        return self.jwt_secret_key == DEV_JWT_SECRET_KEY

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
