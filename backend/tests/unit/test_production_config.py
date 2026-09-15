"""The configuration a deployed environment refuses to start on.

Every rule here has the same shape and the same reasoning: the dangerous state
is a deployment that *comes up* with a shipped default still in place, because
then nothing is broken, nobody is paged, and the mistake survives. A warning is
missed. A refusal at start-up is noticed inside a minute.

So this file is the list of things that must stop a production process, and one
test each for the environments where the same value is perfectly fine - a
development machine is not a deployment, and making it behave like one is how
people learn to work around the check.

The two rules that predate this phase - the JWT secret and the selected
provider's credential - live in tests/unit/test_security.py and
tests/unit/test_ai_configuration.py with the rest of their subject matter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import DEV_DATABASE_PASSWORD, Settings
from app.main import create_app

DEPLOYED = ["production", "staging"]
LOCAL = ["development", "test"]

REAL_DATABASE_URL = "postgresql+asyncpg://aiops:a-real-password@db.internal:5432/aiops"


def deployable(environment: str, **overrides: Any) -> Settings:
    """Settings a deployed environment accepts, before the test breaks one.

    ``_env_file=None`` throughout this file: a developer's own backend/.env is
    on disk and git-ignored, and a test asking what the *shipped* defaults are
    must not read it.
    """
    defaults: dict[str, Any] = {
        "app_env": environment,
        "jwt_secret_key": "j" * 48,
        "database_url": REAL_DATABASE_URL,
        "debug": False,
        "anthropic_api_key": "test-anthropic-secret",
        "cors_origins": "https://console.example.test",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_the_baseline_is_actually_deployable() -> None:
    """If this failed, every refusal below would pass for the wrong reason."""
    assert deployable("production").app_env == "production"
    assert deployable("staging").app_env == "staging"


# -- The published database password -------------------------------------------


@pytest.mark.parametrize("environment", DEPLOYED)
def test_a_deployment_refuses_the_development_database_password(environment: str) -> None:
    """It is printed in docker-compose.yml and in app/core/config.py. A
    deployment that kept it has a database anybody who has read the repository
    can open, and everything works, which is the problem."""
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        deployable(
            environment,
            database_url=f"postgresql+asyncpg://aiops:{DEV_DATABASE_PASSWORD}@db:5432/aiops",
        )


@pytest.mark.parametrize("environment", LOCAL)
def test_a_local_environment_keeps_the_convenient_default(environment: str) -> None:
    """The stack has to start on a fresh checkout with no setup."""
    shipped = Settings(_env_file=None, app_env=environment)

    assert DEV_DATABASE_PASSWORD in shipped.database_url


def test_the_password_is_found_wherever_it_appears_in_the_url() -> None:
    """Matched on the whole URL rather than parsed out of it. A password can be
    percent-encoded and carry options after it; the point is to catch the
    copied default, not to reimplement a URL parser."""
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        deployable(
            "production",
            database_url=(
                f"postgresql+asyncpg://user:{DEV_DATABASE_PASSWORD}@host:5432/db?ssl=require"
            ),
        )


# -- Debug ---------------------------------------------------------------------


@pytest.mark.parametrize("environment", DEPLOYED)
def test_a_deployment_refuses_to_run_in_debug(environment: str) -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        deployable(environment, debug=True)


@pytest.mark.parametrize("environment", LOCAL)
def test_debug_is_the_default_where_it_belongs(environment: str) -> None:
    assert Settings(_env_file=None, app_env=environment).debug is True


def test_debug_is_still_not_wired_to_starlette(settings: Settings) -> None:
    """The refusal above guards a setting nothing branches on, on purpose.

    ``create_app`` deliberately does not hand ``DEBUG`` to Starlette, because
    Starlette's debug mode answers an unhandled exception with a traceback and
    never calls the registered 500 handler. The first thing that *does* branch
    on this setting should not find it already true in production.
    """
    assert settings.debug is True
    assert create_app(settings).debug is False


# -- CORS ----------------------------------------------------------------------


@pytest.mark.parametrize("environment", DEPLOYED)
def test_a_deployment_refuses_a_wildcard_origin(environment: str) -> None:
    """``*`` is not a relaxed setting here, it is a broken one: the application
    sends credentials, and Starlette answers a wildcard-with-credentials by
    echoing whatever origin asked - so every site on the internet is allowed to
    call this API as whoever is signed in."""
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        deployable(environment, cors_origins="*")


def test_a_wildcard_among_real_origins_is_still_a_wildcard() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        deployable("production", cors_origins="https://console.example.test, *")


def test_a_deployment_may_trust_no_browser_origin_at_all() -> None:
    """An empty list is a decision, not a mistake: an API with no browser
    client in front of it should allow no cross-origin request."""
    assert deployable("production", cors_origins="").cors_origin_list == []


def test_local_development_may_use_a_wildcard() -> None:
    local = Settings(_env_file=None, app_env="development", cors_origins="*")

    assert "*" in local.cors_origin_list


# -- Interactive documentation -------------------------------------------------


async def status_of(app: FastAPI, path: str) -> int:
    """The status the application actually answers with.

    Asked over HTTP rather than read off ``app.routes``: FastAPI represents an
    included router as one opaque entry there, so the route table cannot tell
    you whether ``/api/v1/auth/login`` is still mounted. A request can.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return (await client.get(path)).status_code


def test_documentation_is_served_by_default(settings: Settings) -> None:
    """A platform nobody can read the contract of is not much of a platform."""
    app = create_app(settings)

    assert settings.docs_enabled is True
    assert app.docs_url == "/docs"
    assert app.openapi_url == f"{settings.api_v1_prefix}/openapi.json"


async def test_the_default_documentation_really_answers(settings: Settings) -> None:
    app = create_app(settings)

    assert await status_of(app, "/docs") == 200
    assert await status_of(app, f"{settings.api_v1_prefix}/openapi.json") == 200


async def test_turning_it_off_removes_the_schema_as_well_as_the_page(
    settings: Settings,
) -> None:
    """``/docs`` is only a renderer for ``openapi.json``. Leaving the schema up
    would publish every route, every field and every error code to anyone who
    asked for it."""
    app = create_app(settings.model_copy(update={"docs_enabled": False}))

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None

    assert await status_of(app, "/docs") == 404
    assert await status_of(app, "/redoc") == 404
    assert await status_of(app, f"{settings.api_v1_prefix}/openapi.json") == 404


async def test_turning_it_off_leaves_the_api_and_the_probes_alone(
    settings: Settings,
) -> None:
    """Hiding the map does not move the roads."""
    app = create_app(settings.model_copy(update={"docs_enabled": False}))

    assert await status_of(app, "/health") == 200
    # 405 rather than 404: the login route is mounted, it just does not take a
    # GET. Which is the point - the route is still there.
    assert await status_of(app, f"{settings.api_v1_prefix}/auth/login") == 405


async def test_the_root_stops_advertising_documentation_that_is_gone(
    settings: Settings,
) -> None:
    """A pointer to a 404 is worse than no pointer: it sends somebody looking
    for a proxy problem that does not exist."""
    app = create_app(settings.model_copy(update={"docs_enabled": False}))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        body = (await client.get("/")).json()

    assert "docs" not in body
    assert body["health"] == "/health"


# -- The deployment template ---------------------------------------------------


def template_settings() -> dict[str, str]:
    """Every setting the deployment template names, as the template leaves it."""
    template = (Path(__file__).resolve().parents[3] / ".env.production.example").read_text(
        encoding="utf-8"
    )
    known = set(Settings.model_fields)
    values: dict[str, str] = {}
    for line in template.splitlines():
        match = re.match(r"^([A-Z_]+)=(.*)$", line)
        if match and match.group(1).lower() in known and match.group(2):
            values[match.group(1).lower()] = match.group(2)
    return values


def test_the_template_configures_the_application_it_claims_to() -> None:
    """A template full of variables nothing reads would be worse than none: it
    would look like configuration and do nothing."""
    assert len(template_settings()) >= 20


def test_a_copied_but_unedited_template_cannot_start() -> None:
    """The last line of defence, and the failure mode worth having.

    Somebody copies the template, changes what they think matters, and deploys.
    Every secret in it is the literal REPLACE_ME - eleven characters - so the
    JWT rule alone stops the process before it serves a request signed with a
    key that is in a public repository.
    """
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        Settings(_env_file=None, **template_settings())


# -- Tracing -------------------------------------------------------------------


def test_tracing_is_off_unless_a_deployment_turns_it_on(settings: Settings) -> None:
    assert settings.tracing_enabled is False
    assert create_app(settings).state.tracer.enabled is False


def test_an_enabled_deployment_gets_a_tracer_that_records(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"tracing_enabled": True}))

    assert app.state.tracer.enabled is True


def test_the_sample_ratio_is_bounded_at_both_ends() -> None:
    for ratio in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_env="test", tracing_sample_ratio=ratio)

    for ratio in (0.0, 0.5, 1.0):
        accepted = Settings(_env_file=None, app_env="test", tracing_sample_ratio=ratio)
        assert accepted.tracing_sample_ratio == ratio


def test_every_span_carries_the_deployment_it_came_from(settings: Settings) -> None:
    """Three resource attributes, all of them deployment configuration. None of
    them is a tenant, and the allow-list is what keeps it that way."""
    app = create_app(settings.model_copy(update={"tracing_enabled": True}))

    assert app.state.tracer.resource == {
        "service.name": settings.app_name,
        "service.version": app.version,
        "deployment.environment.name": "test",
    }


def test_each_application_gets_its_own_tracer(settings: Settings) -> None:
    """Same reason as the instruments: two applications in one process must not
    write into each other's telemetry."""
    assert create_app(settings).state.tracer is not create_app(settings).state.tracer
