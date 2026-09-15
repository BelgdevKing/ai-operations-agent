"""The deployment artifacts, checked as files rather than trusted as prose.

Everything here is a property somebody could remove in a one-line edit and
nobody would notice until it mattered: a ``USER`` line that stopped a container
running as root, a ``:?`` that stopped a deployment falling back to the
published development secret, an absent ``ports:`` that kept the database off
the host's interfaces. None of them has a runtime symptom. All of them are
readable from the file.

So this file reads the manifests and asserts what they say. It is the cheapest
kind of test - no container is built and nothing is started, which is why it
runs in the unit suite on a laptop with no Docker installed at all - and it
covers the class of mistake that Docker would not have caught anyway, because
an image that runs as root runs perfectly well.

The image builds themselves happen in CI, where there is a daemon; see
.github/workflows/ci.yml.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # arrives with uvicorn[standard]; see backend/requirements.txt

ROOT = Path(__file__).resolve().parents[3]

BACKEND_DOCKERFILE = ROOT / "infrastructure" / "docker" / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = ROOT / "infrastructure" / "docker" / "frontend" / "Dockerfile"
DEV_COMPOSE = ROOT / "docker-compose.yml"
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
PROD_ENV_TEMPLATE = ROOT / ".env.production.example"
GITIGNORE = ROOT / ".gitignore"

# The variables a deployment must supply. Every one of them either is a secret
# or decides who may talk to the deployment, and none of them has a safe
# default - which is why the manifest must use `${VAR:?...}` rather than
# `${VAR:-something}`.
MUST_BE_SUPPLIED = (
    "JWT_SECRET_KEY",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "POSTGRES_USER",
    "CORS_ORIGINS",
    "NEXT_PUBLIC_API_URL",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compose(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(read(path))
    assert isinstance(loaded, dict)
    return loaded


def stages(dockerfile: Path) -> list[str]:
    """The build stages, in the order the file declares them."""
    return re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", read(dockerfile), flags=re.MULTILINE)


def stage_body(dockerfile: Path, name: str) -> str:
    """One stage's instructions, up to the next FROM."""
    text = read(dockerfile)
    match = re.search(rf"^FROM\s+\S+\s+AS\s+{re.escape(name)}\s*$", text, flags=re.MULTILINE)
    assert match, f"{dockerfile.name} has no stage named {name}"
    rest = text[match.end() :]
    following = re.search(r"^FROM\s", rest, flags=re.MULTILINE)
    return rest[: following.start()] if following else rest


def instructions(dockerfile: Path, name: str) -> str:
    """One stage's instructions with the commentary removed.

    A test about what a build *does* must read what it runs. These Dockerfiles
    explain themselves at length - including, in the production stage, the
    sentence "requirements.txt, not requirements-dev.txt" - and an assertion
    that matched prose would be asserting the wrong thing twice over: it would
    fail on a correct file that mentioned the wrong name, and pass on a broken
    one whose comment had been deleted.
    """
    body = stage_body(dockerfile, name)
    kept = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(kept)


DOCKERFILES = pytest.mark.parametrize(
    "dockerfile", [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE], ids=["backend", "frontend"]
)


# -- Every artifact this phase promises actually exists -------------------------


@pytest.mark.parametrize(
    "path",
    [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE, DEV_COMPOSE, PROD_COMPOSE, PROD_ENV_TEMPLATE],
    ids=lambda path: path.name,
)
def test_the_artifact_is_present(path: Path) -> None:
    assert path.is_file(), f"{path} is missing"


# -- One Dockerfile, two targets ------------------------------------------------


@DOCKERFILES
def test_both_targets_are_declared(dockerfile: Path) -> None:
    """Two images from one file, so the base image, the Python or Node version
    and the unprivileged user cannot drift apart between them."""
    declared = stages(dockerfile)

    assert "development" in declared
    assert "production" in declared


@DOCKERFILES
def test_production_is_the_last_stage(dockerfile: Path) -> None:
    """A ``docker build`` with no ``--target`` builds the last stage. That
    should be the hardened image, not the one with the reloader in it."""
    assert stages(dockerfile)[-1] == "production"


@DOCKERFILES
def test_every_shipped_target_drops_to_an_unprivileged_user(dockerfile: Path) -> None:
    """A container running as root is one kernel bug away from the host."""
    for target in ("development", "production"):
        body = instructions(dockerfile, target)
        users = re.findall(r"^USER\s+(\S+)", body, flags=re.MULTILINE)

        assert users, f"{dockerfile.name}:{target} never leaves root"
        assert users[-1] not in ("root", "0"), f"{dockerfile.name}:{target} ends as root"


@DOCKERFILES
def test_every_shipped_target_has_a_health_check(dockerfile: Path) -> None:
    """Compose waits on these before it starts what depends on them."""
    for target in ("development", "production"):
        assert "HEALTHCHECK" in instructions(dockerfile, target), f"{dockerfile.name}:{target}"


def test_the_production_backend_installs_runtime_dependencies_only() -> None:
    """pytest, mypy and ruff are build tools. An image that carries them is a
    larger image with more to audit and nothing more to offer at runtime."""
    body = instructions(BACKEND_DOCKERFILE, "production")

    assert "requirements.txt" in body
    assert "requirements-dev.txt" not in body


def test_the_production_backend_copies_what_it_runs_and_no_more() -> None:
    """Named directories rather than the whole build context.

    The test suite, the tooling configuration and the README have nothing to do
    at runtime, and every file in an image is a file somebody has to reason
    about when an advisory lands. The migration chain and the two maintenance
    scripts do belong: the migrate job applies one and an operator schedules
    the others.
    """
    body = instructions(BACKEND_DOCKERFILE, "production")
    copied = re.findall(r"^COPY\s+(?:--\S+\s+)*(\S+)\s", body, flags=re.MULTILINE)

    assert "tests" not in copied
    assert "." not in copied
    assert {"app", "alembic", "scripts", "alembic.ini"} <= set(copied)


def test_the_production_backend_does_not_reload() -> None:
    """``--reload`` watches the filesystem and restarts the process. In a
    deployment there is nothing to watch and a restart is an outage."""
    assert "--reload" not in instructions(BACKEND_DOCKERFILE, "production")
    # And the development image still does, because that is its whole point.
    assert "--reload" in instructions(BACKEND_DOCKERFILE, "development")


def test_the_production_backend_checks_readiness_not_just_liveness() -> None:
    """A container that has lost PostgreSQL is not one to send requests to, and
    /health/ready is the endpoint that knows the difference."""
    body = instructions(BACKEND_DOCKERFILE, "production")
    health = body[body.index("HEALTHCHECK") :]

    assert "/health/ready" in health


def test_the_production_backend_shuts_down_gracefully() -> None:
    """On SIGTERM: stop accepting, let in-flight requests finish, then run the
    lifespan shutdown that closes the pools. An agent step mid-flight needs the
    window; without one it becomes an abandoned run."""
    assert "--timeout-graceful-shutdown" in instructions(BACKEND_DOCKERFILE, "production")


def test_the_production_frontend_runs_the_compiled_server() -> None:
    """``next dev`` compiles on demand and watches for changes. The production
    image runs the server that ``output: "standalone"`` produced."""
    body = instructions(FRONTEND_DOCKERFILE, "production")

    assert "server.js" in body
    assert "npm run dev" not in body
    assert "NODE_ENV=production" in body


def test_the_production_frontend_carries_no_npm_tree_or_source() -> None:
    """Three copies out of the builder and nothing else: a smaller image, and a
    much shorter list of things to patch when an advisory lands."""
    body = instructions(FRONTEND_DOCKERFILE, "production")

    assert "npm ci" not in body
    assert "npm install" not in body
    assert body.count("COPY --from=builder") == 3


# -- The development stack still builds the development image -------------------


def test_the_development_stack_names_its_target() -> None:
    """Without this it would build the last stage, which is production - and a
    developer would wonder why their edits stopped showing up."""
    services = compose(DEV_COMPOSE)["services"]

    for name in ("backend", "frontend"):
        assert services[name]["build"]["target"] == "development", name


# -- The deployed stack ---------------------------------------------------------


def test_the_deployed_stack_builds_the_production_target() -> None:
    services = compose(PROD_COMPOSE)["services"]

    for name in ("backend", "frontend", "migrate"):
        assert services[name]["build"]["target"] == "production", name


@pytest.mark.parametrize("variable", MUST_BE_SUPPLIED)
def test_a_required_value_has_no_fallback_in_the_deployed_stack(variable: str) -> None:
    """``${VAR:?...}``, never ``${VAR:-something}``.

    The failure mode this prevents is the only one that matters here: a stack
    that comes up on a value published in this repository. Compose refuses, and
    names the variable, which is a thirty-second fix rather than an incident.
    """
    text = read(PROD_COMPOSE)

    assert f"${{{variable}:?" in text, f"{variable} must be required"
    assert f"${{{variable}:-" not in text, f"{variable} must not have a default"


def test_the_deployed_stack_never_uses_the_development_secrets() -> None:
    """The two values this repository publishes, by name."""
    text = read(PROD_COMPOSE)

    assert "dev-only-insecure-jwt-secret-change-me" not in text
    assert "aiops_dev_password" not in text


def test_the_database_and_cache_are_not_published_to_the_host() -> None:
    """They are reachable on the internal network by the backend and the
    migration job. Publishing a port puts them on the host's interfaces, and on
    a cloud host that is frequently the internet."""
    services = compose(PROD_COMPOSE)["services"]

    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]


def test_the_application_is_published_to_loopback_only() -> None:
    """TLS terminates at a reverse proxy on the host. Nothing here should be
    reachable from another machine directly."""
    services = compose(PROD_COMPOSE)["services"]

    for name in ("backend", "frontend"):
        published = services[name]["ports"]
        assert len(published) == 1, name
        assert published[0].startswith("${" + f"{name.upper()}_BIND:-127.0.0.1" + "}"), name


def test_nothing_in_the_deployed_stack_mounts_source() -> None:
    """A production container runs the code in its image. A bind mount means it
    runs whatever is on the host, which is not what was built or tested."""
    services = compose(PROD_COMPOSE)["services"]

    for name in ("backend", "frontend", "migrate"):
        for volume in services[name].get("volumes", []):
            assert not str(volume).startswith("."), f"{name} mounts {volume}"


def test_debug_is_pinned_off_rather_than_left_to_the_environment() -> None:
    """The application refuses to start with it on in production anyway. Pinned
    here so the refusal is never reached and never has to be explained."""
    assert 'DEBUG: "false"' in read(PROD_COMPOSE)


def test_migrations_are_a_step_of_their_own() -> None:
    """One job, run once, before the application - not something every starting
    container does. N containers starting together would be N concurrent
    ``alembic upgrade head`` runs against one database."""
    services = compose(PROD_COMPOSE)["services"]

    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["backend"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }


def test_no_migration_runs_a_downgrade() -> None:
    """A downgrade drops columns. Automating that during a deployment means an
    unlucky rollback destroys data; docs/deployment.md covers doing it by
    hand, deliberately, with a backup taken first."""
    assert "downgrade" not in read(PROD_COMPOSE)


def test_the_application_waits_for_its_dependencies() -> None:
    services = compose(PROD_COMPOSE)["services"]
    backend = services["backend"]["depends_on"]

    assert backend["postgres"] == {"condition": "service_healthy"}
    assert backend["redis"] == {"condition": "service_healthy"}


def test_the_database_is_given_time_to_stop() -> None:
    """The default ten seconds is how a checkpoint becomes a recovery."""
    assert compose(PROD_COMPOSE)["services"]["postgres"]["stop_grace_period"] == "60s"


# -- Secrets are references, never values ---------------------------------------


CREDENTIAL_SHAPED = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    "path",
    [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE, DEV_COMPOSE, PROD_COMPOSE, PROD_ENV_TEMPLATE],
    ids=lambda path: path.name,
)
def test_no_deployment_artifact_contains_a_credential(path: Path) -> None:
    """None of these files is a place a key may be written. They are all in
    version control, and two of them are copied into images."""
    assert not CREDENTIAL_SHAPED.search(read(path)), path.name


@pytest.mark.parametrize("variable", ["JWT_SECRET_KEY", "POSTGRES_PASSWORD", "ANTHROPIC_API_KEY"])
def test_the_template_ships_a_placeholder_rather_than_a_value(variable: str) -> None:
    """A template with a working value in it is a credential in version
    control that looks like documentation."""
    line = next(
        row for row in read(PROD_ENV_TEMPLATE).splitlines() if row.startswith(f"{variable}=")
    )

    assert line == f"{variable}=REPLACE_ME", line


def test_the_template_tells_you_how_to_generate_each_secret() -> None:
    """Otherwise the shortest path to a running deployment is a short password
    somebody typed."""
    assert "secrets.token_urlsafe" in read(PROD_ENV_TEMPLATE)


def test_a_real_environment_file_cannot_be_committed() -> None:
    """``.env.*`` is ignored and the templates are named back in one at a time,
    so a new ``.env.<anything>`` is ignored by default - which is the right way
    round, because the cost of forgetting is a published credential."""
    ignore = read(GITIGNORE)

    assert ".env.*" in ignore
    assert "!.env.example" in ignore
    assert "!.env.production.example" in ignore


@pytest.mark.parametrize("name", ["backend", "frontend"])
def test_no_environment_file_can_enter_an_image(name: str) -> None:
    """The build context is excluded before Docker ever sees it, so a developer
    with a populated .env on disk cannot bake it into a published image."""
    ignore = read(ROOT / name / ".dockerignore")

    assert ".env" in ignore
    assert ".env.*" in ignore
    assert ".git" in ignore
