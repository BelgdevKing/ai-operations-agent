"""Application settings, loaded from environment variables.

Every setting has a development-friendly default so the application, the test
suite and the tooling all start without a populated environment. Production
values are supplied by the environment; nothing secret is ever hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogFormat = Literal["json", "console"]


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

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

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
