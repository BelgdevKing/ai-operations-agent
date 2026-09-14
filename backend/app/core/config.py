"""Application settings, loaded from environment variables.

Every setting has a development-friendly default so the application, the test
suite and the tooling all start without a populated environment. Production
values are supplied by the environment; nothing secret is ever hard-coded.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "AI Operations Agent Platform"
    app_env: str = "development"
    debug: bool = True
    log_level: str = "INFO"

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000

    # Comma-separated; parsed by cors_origin_list below.
    cors_origins: str = "http://localhost:3000"

    # Data stores.
    # Defaults target a native localhost install; docker-compose.yml overrides
    # the hosts with its service names.
    database_url: str = (
        "postgresql+asyncpg://aiops:aiops_dev_password@localhost:5432/aiops"
    )
    redis_url: str = "redis://localhost:6379/0"

    # Redis is not used by any implemented feature yet, so native development
    # does not need it running. Compose sets this to true, where Redis is part
    # of the stack. When false, an unreachable Redis is reported but does not
    # make the service unready.
    redis_required: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
