"""Provider configuration, the registry, and the handling of credentials.

The security half of this file proves a single claim: an API key set in
configuration cannot reach a log, an exception, a repr, a serialised payload or
test output. Fake credentials throughout - `test-anthropic-secret` and
`test-openai-secret` are not valid anywhere.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.ai.exceptions import LLMConfigurationError, LLMError
from app.ai.models import LLMMessage, LLMRequest, LLMResponse
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.openai import OpenAIProvider
from app.ai.providers.registry import (
    PROVIDERS,
    available_providers,
    create_provider,
    get_provider_class,
)
from app.core.config import Settings
from tests.unit.ai_doubles import (
    ANTHROPIC_KEY,
    OPENAI_KEY,
    anthropic_client,
    anthropic_errors,
    openai_client,
    openai_errors,
)

SECRETS = (ANTHROPIC_KEY, OPENAI_KEY)


def settings(**overrides: Any) -> Settings:
    overrides.setdefault("app_env", "test")
    return Settings(**overrides)


def deployed(environment: str, **overrides: Any) -> Settings:
    """Settings a deployed environment would actually accept.

    Everything a deployment must not be caught with is set here rather than in
    each test: the development JWT secret, the published development database
    password and DEBUG are each refused on their own, and each has its own test
    in tests/unit/test_security.py and tests/unit/test_config.py. This helper
    exists so that a test about provider credentials is only about provider
    credentials.
    """
    return Settings(  # type: ignore[arg-type]
        app_env=environment,
        jwt_secret_key="j" * 48,
        database_url="postgresql+asyncpg://aiops:not-the-default@db:5432/aiops",
        debug=False,
        **overrides,
    )


# -- Defaults -----------------------------------------------------------------


def test_anthropic_is_the_default_provider() -> None:
    assert settings().llm_provider == "anthropic"


def test_each_provider_has_a_default_model() -> None:
    assert settings().anthropic_model
    assert settings().openai_model


def test_the_selected_provider_decides_the_model_and_key() -> None:
    both = settings(
        anthropic_api_key=ANTHROPIC_KEY,
        openai_api_key=OPENAI_KEY,
        anthropic_model="claude-model",
        openai_model="openai-model",
    )

    assert both.llm_model == "claude-model"
    assert both.model_copy(update={"llm_provider": "openai"}).llm_model == "openai-model"


def test_an_unknown_provider_name_is_rejected_by_configuration() -> None:
    with pytest.raises(ValidationError):
        settings(llm_provider="gemini")


def test_there_is_a_timeout_and_it_must_be_positive() -> None:
    """A model call must never be able to hang a request indefinitely."""
    assert settings().llm_timeout_seconds > 0

    with pytest.raises(ValidationError):
        settings(llm_timeout_seconds=0)


def test_no_api_key_is_configured_by_default() -> None:
    """Shipping a placeholder credential would be worse than none."""
    assert settings().anthropic_api_key is None
    assert settings().openai_api_key is None


# -- Only the selected provider needs a credential ----------------------------


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_a_deployment_requires_the_selected_provider_key(environment: str) -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        deployed(environment, llm_provider="anthropic")

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        deployed(environment, llm_provider="openai")


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_the_unselected_provider_key_is_not_required(environment: str) -> None:
    """A deployment using Anthropic should not need an OpenAI account."""
    anthropic_only = deployed(
        environment, llm_provider="anthropic", anthropic_api_key=ANTHROPIC_KEY
    )
    openai_only = deployed(environment, llm_provider="openai", openai_api_key=OPENAI_KEY)

    assert anthropic_only.openai_api_key is None
    assert openai_only.anthropic_api_key is None


def test_the_wrong_provider_key_does_not_satisfy_the_requirement() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        deployed("production", llm_provider="anthropic", openai_api_key=OPENAI_KEY)


def test_development_starts_without_any_key() -> None:
    """The application must be runnable with no vendor account; the failure
    arrives at call time instead."""
    assert settings(app_env="development").llm_api_key is None


def test_configuration_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setenv("OPENAI_MODEL", "some-model")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")

    loaded = Settings(_env_file=None)

    assert loaded.llm_provider == "openai"
    assert loaded.openai_model == "some-model"
    assert loaded.llm_timeout_seconds == 12.5
    assert loaded.llm_api_key is not None
    assert loaded.llm_api_key.get_secret_value() == OPENAI_KEY


# -- Registry -----------------------------------------------------------------


def test_anthropic_resolves() -> None:
    assert get_provider_class("anthropic") is AnthropicProvider


def test_openai_resolves() -> None:
    assert get_provider_class("openai") is OpenAIProvider


def test_an_unknown_provider_fails_with_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        get_provider_class("gemini")

    assert "gemini" in raised.value.message
    assert "anthropic" in raised.value.message


def test_the_registry_lists_what_it_supports() -> None:
    assert available_providers() == ("anthropic", "openai")
    assert set(PROVIDERS) == set(available_providers())


def test_every_registered_name_matches_its_adapter() -> None:
    """Otherwise LLMResponse.provider would disagree with LLM_PROVIDER."""
    for name, provider_class in PROVIDERS.items():
        assert provider_class.name == name


def test_the_factory_builds_the_configured_provider() -> None:
    built = create_provider(settings(anthropic_api_key=ANTHROPIC_KEY))

    assert isinstance(built, AnthropicProvider)


def test_the_factory_honours_the_selection() -> None:
    built = create_provider(settings(llm_provider="openai", openai_api_key=OPENAI_KEY))

    assert isinstance(built, OpenAIProvider)


def test_the_factory_refuses_without_a_credential() -> None:
    """Where development's permissive start-up turns into a clear failure."""
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        create_provider(settings(llm_provider="openai"))


def test_the_factory_passes_the_configured_timeout() -> None:
    built = create_provider(settings(anthropic_api_key=ANTHROPIC_KEY, llm_timeout_seconds=7.0))

    assert built._client.timeout == 7.0  # type: ignore[attr-defined]


# -- Secrets do not leak ------------------------------------------------------


def test_a_key_is_masked_in_the_settings_repr() -> None:
    """A Settings object reaching a log or a traceback must not carry it."""
    loaded = settings(anthropic_api_key=ANTHROPIC_KEY, openai_api_key=OPENAI_KEY)

    for secret in SECRETS:
        assert secret not in repr(loaded)
        assert secret not in str(loaded)
    assert "**********" in repr(loaded.anthropic_api_key)


def test_a_key_is_masked_in_configuration_serialization() -> None:
    loaded = settings(anthropic_api_key=ANTHROPIC_KEY, openai_api_key=OPENAI_KEY)

    dumped = json.dumps(loaded.model_dump(mode="json"))

    for secret in SECRETS:
        assert secret not in dumped


def test_reading_a_key_requires_saying_so() -> None:
    """get_secret_value() is greppable; an accidental read is not silent."""
    loaded = settings(anthropic_api_key=ANTHROPIC_KEY)

    assert isinstance(loaded.anthropic_api_key, SecretStr)
    assert loaded.anthropic_api_key.get_secret_value() == ANTHROPIC_KEY


@pytest.mark.parametrize(
    ("provider_class", "key"), [(AnthropicProvider, ANTHROPIC_KEY), (OpenAIProvider, OPENAI_KEY)]
)
def test_a_key_is_not_in_an_adapter_repr(provider_class: Any, key: str) -> None:
    """The adapter hands the key to the SDK client and keeps no copy of its
    own; its repr is explicit so a future field cannot expose one."""
    built = provider_class(key, timeout_seconds=30.0)

    assert key not in repr(built)
    assert key not in str(built)
    assert key not in str(vars(built))


async def test_a_key_is_not_in_a_translated_error() -> None:
    """Every error the caller sees is built from our own default messages."""
    for client, errors, provider_class, key in (
        (anthropic_client, anthropic_errors(), AnthropicProvider, ANTHROPIC_KEY),
        (openai_client, openai_errors(), OpenAIProvider, OPENAI_KEY),
    ):
        for error in errors.values():
            built = provider_class(key, timeout_seconds=30.0, client=client(create_error=error))
            request = LLMRequest(messages=[LLMMessage.user("hi")], model="m")
            try:
                await built.generate(request)
            except Exception as raised:
                assert key not in str(raised)
                assert key not in repr(raised)
                assert key not in str(getattr(raised, "details", {}))


async def test_a_key_is_not_written_to_the_log_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = LLMRequest(messages=[LLMMessage.user("hi")], model="m")
    built = AnthropicProvider(
        ANTHROPIC_KEY,
        timeout_seconds=30.0,
        client=anthropic_client(create_error=anthropic_errors()["authentication"]),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError):
            await built.generate(request)

    assert caplog.text
    assert ANTHROPIC_KEY not in caplog.text


async def test_a_key_is_not_in_a_successful_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.ai.models import LLMMessage, LLMRequest

    built = AnthropicProvider(ANTHROPIC_KEY, timeout_seconds=30.0, client=anthropic_client())

    with caplog.at_level(logging.DEBUG):
        response = await built.generate(
            LLMRequest(messages=[LLMMessage.user("hi")], model="claude-opus-5")
        )

    assert ANTHROPIC_KEY not in json.dumps(response.model_dump(mode="json"))
    assert ANTHROPIC_KEY not in caplog.text


def test_the_response_model_has_nowhere_to_put_a_credential() -> None:
    """Structural, not incidental: there is no field a key could occupy."""
    fields = set(LLMResponse.model_fields)

    assert not {name for name in fields if "key" in name or "secret" in name or "token" in name}
