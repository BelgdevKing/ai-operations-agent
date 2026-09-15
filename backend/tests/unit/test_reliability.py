"""What happens when a dependency is slow, absent, or lying about being there.

Part 22's subject. Three failure modes were found in the existing code and each
one has its tests here:

1. **A dependency outage read as an application bug.** With PostgreSQL
   unreachable, every API call answered ``500 internal_error`` - the same
   response a null dereference produces. A client cannot tell "retry shortly"
   from "this is broken", and neither can whoever is paged.

2. **A probe that waits instead of failing.** The Redis client was built with
   the library's default timeouts, which are *no timeout*. On a network path
   that drops packets rather than refusing connections, the readiness check
   therefore waits for the operating system - and an instance whose readiness
   never answers is never taken out of rotation, so traffic keeps arriving at
   something that cannot serve it.

3. **Timeouts a deployment could set to anything, or forget.** The database had
   no connect or command timeout at all, and the model call had a floor but no
   ceiling.

The tests below are written against the *boundaries*, not the plumbing: what
counts as "the database is gone", what a session does about it, what a
misconfiguration is refused, and - the one that matters most for a credential -
what an error message is allowed to contain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)

from app.core import cache
from app.core import database as db
from app.core.config import (
    DATABASE_URL_PREFIX,
    MAX_DATABASE_TIMEOUT_SECONDS,
    MAX_LLM_TIMEOUT_SECONDS,
    MAX_REDIS_TIMEOUT_SECONDS,
    MIN_DATABASE_TIMEOUT_SECONDS,
    REDIS_URL_SCHEMES,
    ConfigurationError,
    Settings,
    get_settings,
)
from app.core.exceptions import NotFoundError, ServiceUnavailableError
from app.main import create_app, lifespan

ALEMBIC_ENV = Path(__file__).resolve().parents[2] / "alembic" / "env.py"

# A password that would be unmistakable if it ever escaped into a message.
SECRET = "s3cret-that-must-never-be-echoed"


def settings(**overrides: object) -> Settings:
    overrides.setdefault("app_env", "test")
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def sqlalchemy_error(kind: type[DBAPIError], message: str = "boom") -> DBAPIError:
    """One of SQLAlchemy's wrapped driver errors, built without a driver."""
    return kind("SELECT 1", {}, Exception(message))


# -- What counts as "the database is gone" ------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        sqlalchemy_error(OperationalError),
        sqlalchemy_error(InterfaceError),
        ConnectionRefusedError("refused"),
        ConnectionResetError("reset"),
        OSError("no route to host"),
        TimeoutError("the command timeout fired"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_transport_failure_means_the_database_could_not_serve(error: Exception) -> None:
    assert db.is_unavailable(error) is True


def test_an_invalidated_connection_counts_too() -> None:
    """SQLAlchemy marks the connection dead when the pool had to throw it away,
    which is the same event arriving under a different class."""
    error = sqlalchemy_error(DBAPIError)
    error.connection_invalidated = True

    assert db.is_unavailable(error) is True


@pytest.mark.parametrize(
    "error",
    [
        sqlalchemy_error(IntegrityError),
        sqlalchemy_error(ProgrammingError),
        ValueError("a bug"),
        KeyError("a bug"),
        NotFoundError(),
    ],
    ids=lambda error: type(error).__name__,
)
def test_an_application_fault_is_not_a_dependency_outage(error: Exception) -> None:
    """The half of the classification that is easy to get wrong.

    A unique constraint violated, a query that does not compile, a plain bug -
    reporting any of those as "try again shortly" would mean a defect that
    nobody investigates, because the response says the fault is elsewhere.
    """
    assert db.is_unavailable(error) is False


def test_the_classifier_does_not_guess_from_the_message() -> None:
    """Classification is by type, never by text. A message can say anything,
    including whatever an attacker managed to get into it."""
    assert db.is_unavailable(ValueError("connection refused: could not connect")) is False


# -- What a request does about it ---------------------------------------------


async def test_an_outage_becomes_the_applications_own_service_unavailable() -> None:
    """503 with a stable code, not 500.

    The two are different operational events - one says "try again shortly",
    the other says "this service has a bug" - and a client, a load balancer and
    an on-call engineer each act differently on them.
    """
    session = db.get_session()
    await anext(session)

    with pytest.raises(ServiceUnavailableError) as raised:
        await session.athrow(sqlalchemy_error(OperationalError))

    assert raised.value.status_code == 503
    assert raised.value.code == "service_unavailable"


async def test_the_reply_describes_nothing_about_the_database() -> None:
    """No statement, no driver message, no host, no credential.

    The cause is chained for the log; the message a client sees is the class's
    own, and the error handler drops details for any status above 500.
    """
    session = db.get_session()
    await anext(session)

    with pytest.raises(ServiceUnavailableError) as raised:
        await session.athrow(sqlalchemy_error(OperationalError, f"password={SECRET}"))

    error = raised.value
    assert error.details == {}
    assert SECRET not in error.message
    assert "SELECT" not in error.message
    assert "password" not in error.message.lower()
    # The real cause is still there for the server-side log.
    assert error.__cause__ is not None


async def test_a_real_defect_is_re_raised_exactly_as_it_was() -> None:
    """Not wrapped, not reclassified, not softened into a 503."""
    session = db.get_session()
    await anext(session)
    bug = ValueError("the actual defect")

    with pytest.raises(ValueError) as raised:
        await session.athrow(bug)

    assert raised.value is bug


async def test_a_failing_rollback_does_not_replace_the_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolling back over a connection that has already gone raises again.

    Unguarded, that second exception is the one the caller sees - so the
    request is reported as whatever the rollback hit rather than as what
    actually went wrong, and the useful error is lost.
    """

    async def rollback_fails() -> None:
        raise OSError("the connection is already gone")

    session = db.get_session()
    opened = await anext(session)
    monkeypatch.setattr(opened, "rollback", rollback_fails)

    with pytest.raises(ServiceUnavailableError):
        await session.athrow(sqlalchemy_error(OperationalError))


async def test_nothing_durable_survives_an_outage() -> None:
    """The guarantee the idempotency design rests on.

    A translated failure is still a failed request: the session is rolled back
    before the error is raised, so a run that could not reach the database
    leaves no row - and the caller's idempotency key is still free for the
    retry. Asserted on the session rather than on a table, because the point is
    that the rollback happens *before* the translation, not after it.
    """
    rolled_back = False

    session = db.get_session()
    opened = await anext(session)

    original = opened.rollback

    async def record() -> None:
        nonlocal rolled_back
        rolled_back = True
        await original()

    opened.rollback = record  # type: ignore[method-assign]

    with pytest.raises(ServiceUnavailableError):
        await session.athrow(sqlalchemy_error(OperationalError))

    assert rolled_back is True


# -- The engine is built with bounds ------------------------------------------


def test_the_engine_bounds_both_connecting_and_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pool_timeout`` is not one of these.

    It limits how long a caller waits for an *existing* pooled connection to
    come free, which does nothing at all when the server is unreachable. These
    two are what end that wait.
    """
    captured: dict[str, object] = {}

    def spy(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db, "create_async_engine", spy)
    db.create_engine(
        settings(database_connect_timeout_seconds=7, database_command_timeout_seconds=11)
    )

    assert captured["connect_args"] == {"timeout": 7.0, "command_timeout": 11.0}


def test_migrations_do_not_inherit_the_command_timeout() -> None:
    """A schema change on a large table legitimately takes minutes.

    Alembic builds its own engine in alembic/env.py rather than calling
    ``create_engine`` here, so the application's command timeout cannot cut a
    migration off half way. Asserted on the file, because the property is
    structural: it holds because of which function Alembic calls.
    """
    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "async_engine_from_config" in source
    assert "connect_args" not in source
    assert "from app.core.database import" not in source


def test_the_redis_client_bounds_both_reaching_and_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library defaults both of these to no timeout whatsoever."""
    monkeypatch.setattr(cache, "_settings", settings(redis_timeout_seconds=3.5))
    monkeypatch.setattr(cache, "_client", None)

    kwargs = cache.get_redis().connection_pool.connection_kwargs
    monkeypatch.setattr(cache, "_client", None)

    assert kwargs["socket_connect_timeout"] == 3.5
    assert kwargs["socket_timeout"] == 3.5


# -- A deployment cannot configure the bounds away ----------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_timeout_seconds", MAX_LLM_TIMEOUT_SECONDS + 1),
        ("llm_timeout_seconds", 0),
        ("database_connect_timeout_seconds", MAX_DATABASE_TIMEOUT_SECONDS + 1),
        ("database_connect_timeout_seconds", MIN_DATABASE_TIMEOUT_SECONDS / 2),
        ("database_command_timeout_seconds", MAX_DATABASE_TIMEOUT_SECONDS + 1),
        ("redis_timeout_seconds", MAX_REDIS_TIMEOUT_SECONDS + 1),
        ("redis_timeout_seconds", 0),
    ],
)
def test_an_impossible_timeout_is_refused_at_start_up(field: str, value: float) -> None:
    """Bounded at both ends, and for different reasons.

    Above the ceiling, one request holds a worker and a connection for longer
    than anybody will wait. Below the floor, a value that "works" on an idle
    laptop fails the first time a real report runs - which is the kind of
    setting that only breaks in production.
    """
    with pytest.raises(PydanticValidationError):
        settings(**{field: value})


def test_the_documented_defaults_are_inside_their_bounds() -> None:
    """If this failed, every test above would pass for the wrong reason."""
    shipped = settings()

    assert 0 < shipped.llm_timeout_seconds <= MAX_LLM_TIMEOUT_SECONDS
    assert (
        MIN_DATABASE_TIMEOUT_SECONDS
        <= shipped.database_connect_timeout_seconds
        <= MAX_DATABASE_TIMEOUT_SECONDS
    )
    assert (
        MIN_DATABASE_TIMEOUT_SECONDS
        <= shipped.database_command_timeout_seconds
        <= MAX_DATABASE_TIMEOUT_SECONDS
    )
    assert 0 < shipped.redis_timeout_seconds <= MAX_REDIS_TIMEOUT_SECONDS


def test_a_connect_timeout_is_not_the_same_setting_as_a_pool_timeout() -> None:
    """Both exist, and conflating them is the mistake this guards against."""
    shipped = settings()

    assert shipped.database_pool_timeout > 0
    assert shipped.database_connect_timeout_seconds > 0


# -- A mistyped dependency URL fails at boot, and says nothing ----------------


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://aiops:{SECRET}@db:5432/aiops",
        f"postgres://aiops:{SECRET}@db:5432/aiops",
        f"mysql+aiomysql://aiops:{SECRET}@db:3306/aiops",
        "",
    ],
)
def test_a_database_url_with_the_wrong_driver_is_refused(url: str) -> None:
    """``postgresql://`` is the spelling people have in their fingers, and it
    selects the *synchronous* driver this application does not use. Unchecked,
    the failure is an exception on the first query rather than anything a
    deployment notices at boot."""
    with pytest.raises(PydanticValidationError, match="DATABASE_URL"):
        settings(database_url=url)


def boot(monkeypatch: pytest.MonkeyPatch, **environment: str) -> ConfigurationError:
    """Start the process the way a deployment does, and return the refusal.

    Through ``get_settings`` rather than ``Settings(...)`` on purpose: the
    former is the only place a deployment's configuration is actually read, and
    therefore the only place whose error message a human ever sees.
    """
    monkeypatch.setenv("APP_ENV", "test")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as raised:
            get_settings()
        return raised.value
    finally:
        get_settings.cache_clear()


def test_a_refusal_names_the_setting_and_what_it_should_be(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error nobody can act on is an outage with extra steps."""
    refusal = boot(monkeypatch, DATABASE_URL=f"postgresql://aiops:{SECRET}@db:5432/aiops")

    assert "DATABASE_URL" in str(refusal)
    assert DATABASE_URL_PREFIX in str(refusal)


def test_a_refusal_never_quotes_the_value_it_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this exists for, and it was not in the validators.

    Pydantic appends the input it rejected to every message. For a field that
    input is the field's value; for a model validator it is the whole settings
    dictionary - so mistyping one setting printed the ones beside it, API key
    included, into the first lines of the process's log. Long values are
    abbreviated in the middle, which makes it worse rather than better: whether
    a credential is published depends on how long it happens to be.
    """
    refusal = boot(monkeypatch, DATABASE_URL=f"postgresql://aiops:{SECRET}@db:5432/aiops")
    rendered = str(refusal)

    assert SECRET not in rendered
    assert "aiops:" not in rendered
    assert not re.search(r"://[^/\s]*:[^@/\s]+@", rendered)


def test_a_refusal_does_not_smuggle_the_value_out_in_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``raise ... from None``, because a chained cause is printed too.

    Suppressing the message but leaving the original exception attached would
    put the values back into every traceback that renders it, which is the only
    place most people ever read them.
    """
    refusal = boot(monkeypatch, DATABASE_URL=f"postgresql://aiops:{SECRET}@db:5432/aiops")

    assert refusal.__cause__ is None
    assert refusal.__suppress_context__ is True
    assert SECRET not in repr(refusal.__context__)


def test_a_refusal_carries_every_problem_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixing configuration one restart at a time is how an outage gets long."""
    refusal = boot(
        monkeypatch,
        DATABASE_URL="mysql+aiomysql://h/db",
        REDIS_URL="http://cache:6379",
    )
    rendered = str(refusal)

    assert "DATABASE_URL" in rendered
    assert "REDIS_URL" in rendered
    assert "2 configuration problems" in rendered


@pytest.mark.parametrize(
    "environment",
    [
        {"DATABASE_URL": f"postgresql://u:{SECRET}@h/db"},
        {"REDIS_URL": f"memcached://u:{SECRET}@h"},
        {"APP_ENV": "production", "JWT_SECRET_KEY": SECRET},
        {"METRICS_ENABLED": "true", "METRICS_TOKEN": SECRET[:4]},
        {"LLM_TIMEOUT_SECONDS": "999999", "ANTHROPIC_API_KEY": SECRET},
        {"APP_ENV": "production", "CORS_ORIGINS": "*", "ANTHROPIC_API_KEY": SECRET},
    ],
    ids=["database", "redis", "jwt", "metrics", "llm-timeout", "cors"],
)
def test_no_refusal_anywhere_echoes_a_secret(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    """One sweep over every setting that holds or embeds a credential.

    Written as a loop rather than as separate assertions so that a setting
    added later with the same problem is caught by a test that already exists.
    """
    refusal = boot(monkeypatch, **environment)

    assert SECRET not in str(refusal)


@pytest.mark.parametrize("url", ["http://cache:6379", f"memcached://{SECRET}@cache", ""])
def test_a_redis_url_with_an_unknown_scheme_is_refused(url: str) -> None:
    with pytest.raises(PydanticValidationError, match="REDIS_URL"):
        settings(redis_url=url)


def test_the_redis_url_is_never_quoted_back() -> None:
    with pytest.raises(PydanticValidationError) as raised:
        settings(redis_url=f"memcached://user:{SECRET}@cache:6379")

    rendered = str(raised.value)
    assert SECRET not in rendered
    assert all(scheme in rendered for scheme in REDIS_URL_SCHEMES)


@pytest.mark.parametrize("scheme", REDIS_URL_SCHEMES)
def test_every_documented_redis_scheme_is_accepted(scheme: str) -> None:
    """The list in the error message has to be one that actually works."""
    assert settings(redis_url=f"{scheme}cache:6379/0").redis_url.startswith(scheme)


# -- Shutting down -------------------------------------------------------------


async def test_shutdown_releases_both_dependency_clients(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped process must not leave connections open behind it.

    Pooled PostgreSQL connections and the Redis client both outlive a request
    by design; nothing else closes them. A rolling restart that leaked them
    would exhaust the database's connection limit one deploy at a time, and the
    symptom would appear on an instance nobody had touched.
    """
    released: list[str] = []

    async def note_engine() -> None:
        released.append("engine")

    async def note_redis() -> None:
        released.append("redis")

    monkeypatch.setattr(db, "dispose_engine", note_engine)
    monkeypatch.setattr(cache, "close_redis", note_redis)

    async with lifespan(create_app(settings)):
        assert released == [], "nothing is released while the process is serving"

    assert released == ["engine", "redis"]


async def test_shutdown_completes_even_with_every_dependency_absent(
    settings: Settings,
) -> None:
    """Shutdown must not need the things it is shutting down.

    Both clients connect lazily, so a process that never reached PostgreSQL or
    Redis has nothing to close - and a shutdown path that assumed otherwise
    would hang exactly when a deployment was already going wrong.
    """
    unreachable = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:5999/db"}
    )

    async with lifespan(create_app(unreachable)):
        pass


def test_nothing_in_the_application_starts_work_that_outlives_a_request() -> None:
    """Why the shutdown story is this short, asserted rather than assumed.

    There is no background task, no scheduler and no worker anywhere in the
    application: every unit of work belongs to the request that asked for it,
    which is what makes "stop accepting, drain, close" a complete description
    of shutdown. A ``create_task`` added later would break that silently - the
    process would exit with work in flight - so it fails here instead.

    The maintenance scripts in scripts/ are deliberately outside this: they are
    separate processes an operator schedules, not work this one starts.
    """
    spawning = re.compile(r"\b(?:asyncio\.)?(?:create_task|ensure_future)\s*\(")
    offenders = [
        path.relative_to(Path(__file__).resolve().parents[2])
        for path in (Path(__file__).resolve().parents[2] / "app").rglob("*.py")
        if spawning.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"background work would change the shutdown contract: {offenders}"


def test_the_request_path_never_waits_without_a_bound(settings: Settings) -> None:
    """Every external wait this process makes has a ceiling somebody chose.

    Collected in one place so the list is reviewable: if a new dependency
    arrives without a timeout, the gap is visible here rather than discovered
    when a worker stops answering.
    """
    bounds = {
        "model call": settings.llm_timeout_seconds,
        "tool execution": settings.tool_timeout_seconds,
        "database connect": settings.database_connect_timeout_seconds,
        "database command": settings.database_command_timeout_seconds,
        "database pool wait": float(settings.database_pool_timeout),
        "redis": settings.redis_timeout_seconds,
    }

    for what, seconds in bounds.items():
        assert seconds > 0, what
        assert seconds <= MAX_LLM_TIMEOUT_SECONDS, f"{what} may outlast every other bound"
