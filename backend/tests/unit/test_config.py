"""Application and environment configuration."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


@pytest.fixture
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every Settings variable from the process environment.

    The names come from the model's own fields rather than a hard-coded list,
    so adding a setting cannot quietly reintroduce the leak this guards
    against. Matching is case-insensitive because Settings is.
    """
    field_names = {name.upper() for name in Settings.model_fields}
    for key in list(os.environ):
        if key.upper() in field_names:
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_development_friendly(isolated_environment: None) -> None:
    """A bare checkout must produce a usable configuration.

    Both ambient sources are shut out: the fixture clears the environment, and
    ``_env_file=None`` ignores any backend/.env. Otherwise this asserts
    whatever the machine happens to be configured with - it passed locally
    only because the developer's DATABASE_URL also points at localhost, and
    failed under Docker, where Compose sets the host to "postgres".
    """
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.backend_port == 8000
    assert "localhost" in settings.database_url
    assert settings.redis_required is False


def test_values_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BACKEND_PORT", "9001")
    monkeypatch.setenv("REDIS_REQUIRED", "true")

    settings = Settings()

    assert settings.app_env == "production"
    assert settings.backend_port == 9001
    assert settings.redis_required is True
    assert settings.is_production is True


def test_environment_names_are_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")  # not one of the allowed values

    with pytest.raises(ValidationError):
        Settings()


def test_cors_origins_parse_into_a_list() -> None:
    settings = Settings(cors_origins="http://a.test, http://b.test ,")

    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_cors_origins_handle_a_single_value() -> None:
    assert Settings(cors_origins="http://only.test").cors_origin_list == ["http://only.test"]


def test_log_level_is_normalised() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(log_level="chatty")


def test_pool_settings_must_be_sane() -> None:
    with pytest.raises(ValidationError):
        Settings(database_pool_size=0)


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repository's .env files carry keys for features not yet built."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-used-yet")

    Settings()  # must not raise


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()
