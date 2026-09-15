"""Application settings, loaded from environment variables.

Every setting has a development-friendly default so the application, the test
suite and the tooling all start without a populated environment. Production
values are supplied by the environment; nothing secret is ever hard-coded.
"""

from __future__ import annotations

from functools import cached_property, lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.observability.pricing import PriceBook

Environment = Literal["development", "staging", "production", "test"]
LogFormat = Literal["json", "console"]

# Only HMAC algorithms are accepted. Restricting the set is what prevents an
# algorithm-confusion attack, where a token claims "alg": "none" or an
# asymmetric algorithm that turns the public key into a signing key.
JWTAlgorithm = Literal["HS256", "HS384", "HS512"]

# Deliberately obvious. Never use this outside local development.
DEV_JWT_SECRET_KEY = "dev-only-insecure-jwt-secret-change-me"

# The password the development stack ships with, in the repository, in plain
# sight. Refused in a deployed environment for the same reason as the JWT key
# above: a default that is published is not a secret, and the way that mistake
# reaches production is that nothing ever objected to it.
DEV_DATABASE_PASSWORD = "aiops_dev_password"

# Hard ceilings on the agent runtime. A deployment may be stricter than these;
# it may not be more permissive, so a misconfigured environment variable cannot
# turn one request into an unbounded spend. Mirrored in app/agents/models.py,
# which cannot import this module without a cycle.
MAX_AGENT_STEPS = 32
MAX_AGENT_OUTPUT_TOKENS = 8_192

# A tool may not be given an unbounded deadline by configuration either.
MAX_TOOL_TIMEOUT_SECONDS = 300.0

# Nor may a model call, nor a database one. Every external operation this
# process waits on has a ceiling it cannot be configured past, because the
# failure mode of an unbounded timeout is not an error - it is a worker holding
# a connection and a request slot until somebody notices.
#
# The floors matter as much as the ceilings. A one-second database timeout
# would "work" on an idle laptop and fail the first time a usage report ran
# over a real dataset, which is the kind of setting that only breaks in
# production.
MAX_LLM_TIMEOUT_SECONDS = 300.0
MIN_DATABASE_TIMEOUT_SECONDS = 1.0
MAX_DATABASE_TIMEOUT_SECONDS = 120.0
MIN_REDIS_TIMEOUT_SECONDS = 0.1
MAX_REDIS_TIMEOUT_SECONDS = 30.0

# Drivers this application knows how to talk to. Checked at start-up rather
# than at first use: a typo in a scheme is a deployment that looks healthy
# until the first request, and by then it is a 500 rather than a boot failure.
DATABASE_URL_PREFIX = "postgresql+asyncpg://"
REDIS_URL_SCHEMES = ("redis://", "rediss://", "unix://")

# How long an unfinished agent run may sit untouched before the abandonment
# sweep gives up on it. A ceiling rather than a preference: a deployment that
# set this to a week would keep failed runs holding their idempotency keys for a
# week, and nothing is recoverable after a day that was not recoverable after an
# hour. The floor stops a misconfiguration sweeping runs that are simply slow.
MIN_AGENT_RUN_STALE_SECONDS = 60
MAX_AGENT_RUN_STALE_SECONDS = 86_400

# Ceilings on a workflow definition. A definition is written by a user of the
# platform, so every one of these is a limit on what somebody else's document
# may cost this deployment: how many steps it may contain, how much the document
# itself may weigh, and how much data it may carry between steps.
MAX_WORKFLOW_STEPS = 64
MAX_WORKFLOW_DEFINITION_BYTES = 262_144
MAX_WORKFLOW_PAYLOAD_BYTES = 262_144

# How long a pending approval may wait for a person before it lapses. Bounded at
# both ends and for different reasons: below the floor an approval could expire
# before anybody was plausibly told about it, and above the ceiling a paused run
# holds its conversation, its claimed execution and its place in the queue for
# longer than anyone will remember why it is there. A month is already generous
# for "somebody will get to this".
MIN_APPROVAL_EXPIRATION_SECONDS = 60
MAX_APPROVAL_EXPIRATION_SECONDS = 2_592_000

# How far back a single usage report may reach. Usage is aggregated from the
# execution tables at read time, and the indexes that make that fast are on
# (organization_id, created_at) - so an unbounded window is the one query shape
# that would not use them. A quarter is long enough for "last month" and
# "last quarter", which is what anybody actually asks.
MAX_USAGE_WINDOW_DAYS = 366
DEFAULT_USAGE_WINDOW_DAYS = 92

# Most groups one usage report may return. A report is a summary; anything that
# needs more rows than this is an export, and an export is a different feature
# with different authorization.
MAX_USAGE_GROUPS = 200

# Which vendor serves model calls. Only the selected one needs a credential.
LLMProviderName = Literal["anthropic", "openai"]

# Below this, a brute-forced HMAC key is within reach.
MIN_PRODUCTION_SECRET_LENGTH = 32

# A metrics token is compared in constant time, so its only real defence is
# length. Short enough to type, long enough not to be guessed.
MIN_METRICS_TOKEN_LENGTH = 16


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

    docs_enabled: bool = Field(
        default=True,
        description="Whether the interactive API documentation (/docs, /redoc) "
        "and the OpenAPI schema are served. On by default, because a platform "
        "nobody can read the contract of is not much of a platform; the "
        "production manifest turns it off, because a schema of every route, "
        "every field and every error code is a map, and a deployment gets to "
        "decide who is handed one.",
    )

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
    database_connect_timeout_seconds: float = Field(
        default=10.0,
        ge=MIN_DATABASE_TIMEOUT_SECONDS,
        le=MAX_DATABASE_TIMEOUT_SECONDS,
        description="How long opening a new connection may take. Distinct from "
        "database_pool_timeout, which bounds waiting for an *existing* pooled "
        "connection and does nothing when the server is unreachable. Without "
        "this the wait is the operating system's TCP timeout, which is minutes "
        "on a path that drops packets rather than refusing them - long enough "
        "for a readiness probe to hang instead of reporting unready.",
    )
    database_command_timeout_seconds: float = Field(
        default=30.0,
        ge=MIN_DATABASE_TIMEOUT_SECONDS,
        le=MAX_DATABASE_TIMEOUT_SECONDS,
        description="How long one statement may take before the driver gives "
        "up on it. A server that accepted the connection and then stopped "
        "answering is the case this exists for: nothing else in the stack ends "
        "that wait. Migrations are unaffected - Alembic builds its own engine, "
        "so a long schema change cannot be cut off by this.",
    )

    # -- Redis ---------------------------------------------------------------
    # Redis is not used by any implemented feature yet, so native development
    # does not need it running. Compose sets redis_required to true, where Redis
    # is part of the stack. When false, an unreachable Redis is reported but
    # does not make the service unready.
    redis_url: str = "redis://localhost:6379/0"
    redis_required: bool = False
    redis_timeout_seconds: float = Field(
        default=2.0,
        ge=MIN_REDIS_TIMEOUT_SECONDS,
        le=MAX_REDIS_TIMEOUT_SECONDS,
        description="Ceiling on connecting to Redis and on one command. Short, "
        "because the only thing Redis is used for today is the readiness "
        "check, and a probe that waits is worse than a probe that fails: an "
        "instance whose readiness never answers is never taken out of "
        "rotation. The client library defaults both of these to no timeout at "
        "all.",
    )

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

    # -- Language models -----------------------------------------------------
    # Credentials are SecretStr: printing a Settings object, dumping it to
    # JSON, or letting one reach a log or a traceback yields "**********"
    # rather than the key. Reading the real value requires an explicit
    # .get_secret_value(), which is easy to grep for in review.
    #
    # Only the selected provider needs a key. The validator below refuses to
    # start a deployed environment without it; development starts regardless
    # and fails at call time with LLMConfigurationError, so the application is
    # still runnable with no vendor account.
    llm_provider: LLMProviderName = "anthropic"
    llm_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=MAX_LLM_TIMEOUT_SECONDS,
        description="Per-call ceiling passed to the provider SDK, so a model "
        "call can never hang a request indefinitely. Bounded above as well as "
        "below: a deployment may be stricter than the ceiling, never more "
        "permissive, so a mistyped value cannot hold a worker for an hour.",
    )

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.5"

    # Gateway retry policy. The gateway is the only thing that retries: the
    # provider SDKs are constructed with their own retries disabled, so the
    # worst case is exactly llm_max_retries + 1 calls.
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        description="Retries after the first attempt. 0 disables retrying.",
    )
    llm_retry_max_delay_seconds: float = Field(
        default=8.0,
        gt=0,
        description="Ceiling on any single backoff wait, and on how long a "
        "provider's retry-after hint may be before the gateway gives up "
        "instead of holding the caller.",
    )

    # Models an API caller may request by name, comma-separated. Empty means
    # only the configured model is allowed, which is the safe default: without
    # it a client could name an expensive model and bill the deployment for it.
    llm_allowed_models: str = ""

    # -- Agent runtime ---------------------------------------------------------
    #
    # Every limit a run obeys comes from here. None of them is reachable from a
    # request: an agent's configuration is the server's, so a caller can neither
    # widen a budget nor spend more of someone else's money than this allows.
    agent_max_steps: int = Field(
        default=4,
        ge=0,
        le=MAX_AGENT_STEPS,
        description="Model calls one run may make. The runtime stops with an "
        "error rather than looping past it.",
    )
    agent_max_output_tokens: int = Field(
        default=2_048,
        ge=1,
        le=MAX_AGENT_OUTPUT_TOKENS,
        description="Ceiling on what one step may generate.",
    )
    agent_max_messages: int = Field(
        default=50,
        ge=1,
        description="Conversation turns one run accepts, the system prompt aside.",
    )
    agent_max_conversation_characters: int = Field(
        default=50_000,
        ge=1,
        description="Total size of the conversation handed to a run.",
    )
    agent_run_stale_after_seconds: int = Field(
        default=900,
        ge=MIN_AGENT_RUN_STALE_SECONDS,
        le=MAX_AGENT_RUN_STALE_SECONDS,
        description="How long a pending or running agent run may go without "
        "progress before it is marked failed with agent_run_abandoned. There is "
        "no background worker, so a run whose request died has nothing to "
        "continue it; mid-execution resume is deliberately not attempted. Runs "
        "awaiting approval are never swept - they are paused on purpose.",
    )

    # -- Workflow engine -------------------------------------------------------
    #
    # A workflow definition is data somebody wrote, so every limit here bounds
    # what one document may do to a request. None of them is reachable from a
    # definition or from a run's input.
    workflow_max_steps: int = Field(
        default=32,
        ge=1,
        le=MAX_WORKFLOW_STEPS,
        description="Steps one definition may contain. Checked at activation, "
        "so an oversized workflow is refused before anybody can start it.",
    )
    workflow_max_executed_steps: int = Field(
        default=32,
        ge=1,
        le=MAX_WORKFLOW_STEPS,
        description="Steps one run may execute. The graph is validated acyclic, "
        "so this should never be reached; it is the guard that makes 'should' "
        "unnecessary.",
    )
    workflow_max_definition_bytes: int = Field(
        default=65_536,
        ge=1,
        le=MAX_WORKFLOW_DEFINITION_BYTES,
        description="Serialised size of one definition document.",
    )
    workflow_max_input_bytes: int = Field(
        default=16_384,
        ge=1,
        le=MAX_WORKFLOW_PAYLOAD_BYTES,
        description="Serialised size of the input one run may be started with. "
        "Workflow input is untrusted, and this is the first thing that bounds it.",
    )
    workflow_max_output_bytes: int = Field(
        default=65_536,
        ge=1,
        le=MAX_WORKFLOW_PAYLOAD_BYTES,
        description="Serialised size of what one step may produce. A larger "
        "result is refused rather than truncated: a silently shortened result "
        "is one a later condition would branch on without anybody knowing.",
    )
    workflow_max_input_depth: int = Field(
        default=8,
        ge=1,
        le=32,
        description="How deeply nested workflow input may be. A deeply nested "
        "payload is cheap to send and expensive to walk.",
    )
    workflow_run_stale_after_seconds: int = Field(
        default=900,
        ge=MIN_AGENT_RUN_STALE_SECONDS,
        le=MAX_AGENT_RUN_STALE_SECONDS,
        description="How long a pending or running workflow run may go without "
        "progress before it is marked failed with workflow_run_abandoned. The "
        "same reasoning as the agent runtime's: there is no background worker, "
        "and a run whose request died has nothing to continue it. Runs awaiting "
        "approval are never swept.",
    )

    # -- Human approval --------------------------------------------------------
    approval_expiration_seconds: int = Field(
        default=86_400,
        ge=MIN_APPROVAL_EXPIRATION_SECONDS,
        le=MAX_APPROVAL_EXPIRATION_SECONDS,
        description="How long a pending approval waits for a person before it "
        "lapses. The expiry is stamped on the row when the approval is "
        "requested, so changing this never moves a deadline somebody has "
        "already been given. An expired approval can no longer be decided and "
        "the action it gated never runs; the run it paused is failed with "
        "approval_expired rather than left waiting forever.",
    )

    # -- Observability, usage and cost -----------------------------------------
    #
    # Metrics are process-wide and carry no tenant dimension, so the endpoint
    # that exposes them is protected by a shared token rather than by a user
    # session: the caller is a scraper, not a person, and it has no
    # organization to be scoped to.
    metrics_enabled: bool = Field(
        default=False,
        description="Whether GET /metrics is served at all. Off by default: an "
        "exposition endpoint is a thing to enable deliberately, and a "
        "deployment that has not thought about who may scrape it should not "
        "have one.",
    )
    metrics_token: str | None = Field(
        default=None,
        min_length=MIN_METRICS_TOKEN_LENGTH,
        description="Shared secret a scraper presents as 'Authorization: "
        "Bearer <token>'. Required whenever metrics are enabled - see the "
        "validator; there is no unauthenticated mode.",
    )

    # Tracing answers "where did this one request go", which makes it the
    # telemetry most likely to carry something it should not. What it may carry
    # is an allow-list in app/observability/names.py; what follows is only
    # whether it runs at all.
    tracing_enabled: bool = Field(
        default=False,
        description="Whether spans are recorded. Off by default. While it is "
        "off no span is built, no trace id is bound to the request context and "
        "the logs are byte-for-byte what they were before tracing existed.",
    )
    tracing_sample_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Fraction of traces recorded, decided from the trace id so "
        "that every service in one trace reaches the same answer. 1.0 records "
        "everything, which is right until the volume says otherwise. A trace "
        "whose caller already decided to record it is recorded regardless - "
        "half a trace is worse than none.",
    )

    llm_pricing: str = Field(
        default="",
        description="The price book, as JSON. Empty by default, which is the "
        "honest state for a deployment nobody has priced: usage is reported in "
        "full and cost is reported as unknown. See "
        "app/observability/pricing.py for the shape - note that prices are "
        "quoted as strings, because a JSON number is a float by the time "
        "Python has read it.",
    )

    usage_window_days: int = Field(
        default=DEFAULT_USAGE_WINDOW_DAYS,
        ge=1,
        le=MAX_USAGE_WINDOW_DAYS,
        description="Longest period one usage report may cover. A ceiling on "
        "how much of the execution history a single query walks.",
    )

    # -- Tool framework --------------------------------------------------------
    #
    # A tool talks to systems outside this process, so both limits below exist
    # to stop one slow or chatty dependency holding a request open or filling a
    # conversation with a database dump.
    tool_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=MAX_TOOL_TIMEOUT_SECONDS,
        description="Deadline for one tool execution, unless the tool sets a "
        "shorter one of its own. The framework never waives it.",
    )
    tool_max_result_bytes: int = Field(
        default=32_768,
        ge=1,
        description="Ceiling on a serialised tool result. A larger one is "
        "refused rather than truncated, so nothing silently loses data.",
    )

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

    @field_validator("database_url")
    @classmethod
    def _database_url_must_name_the_driver(cls, value: str) -> str:
        """Catch a mistyped database URL at start-up, not at the first request.

        ``postgresql://`` is the spelling people have in their fingers and it
        selects SQLAlchemy's *synchronous* driver, which this application does
        not use - the failure is an exception on the first query rather than
        anything a deployment would notice at boot.

        **The value is never echoed.** A database URL carries the password, so
        a message quoting it would put a credential into the log line, the
        traceback and whatever collects them. Naming the setting and the
        expected prefix is enough to fix it.
        """
        if not value.startswith(DATABASE_URL_PREFIX):
            raise ValueError(
                f"DATABASE_URL must start with {DATABASE_URL_PREFIX!r}. "
                "The value is not shown because it contains the password."
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _redis_url_must_name_a_known_scheme(cls, value: str) -> str:
        """The same rule for Redis, and the same silence about the value."""
        if not value.startswith(REDIS_URL_SCHEMES):
            raise ValueError(
                f"REDIS_URL must start with one of {list(REDIS_URL_SCHEMES)}. "
                "The value is not shown because it may contain a password."
            )
        return value

    @model_validator(mode="after")
    def _refuse_the_published_database_password(self) -> Settings:
        """Refuse a deployed environment pointed at the development password.

        ``aiops_dev_password`` is in ``docker-compose.yml`` and in this file. A
        deployment that kept it has a database anyone who has read the
        repository can open, and the failure mode is silent: everything works.
        Matched on the URL rather than parsed out of it, because a password can
        be percent-encoded and the point is to catch the copied default, not to
        reimplement a URL parser.
        """
        if self.app_env in ("production", "staging"):
            if DEV_DATABASE_PASSWORD in self.database_url:
                raise ValueError(
                    "DATABASE_URL still contains the development password. "
                    f"Set a real one before running in {self.app_env}."
                )
        return self

    @model_validator(mode="after")
    def _refuse_debug_in_a_deployed_environment(self) -> Settings:
        """``DEBUG`` is a development switch, and deployment is not development.

        Nothing branches on it today - ``create_app`` deliberately does not hand
        it to Starlette, because Starlette's debug mode answers an unhandled
        exception with a traceback and bypasses the error envelope. That is
        exactly why it is refused here rather than ignored: the first thing that
        *does* branch on it should not find it already true in production.
        """
        if self.app_env in ("production", "staging") and self.debug:
            raise ValueError(f"DEBUG must be false in {self.app_env}.")
        return self

    @model_validator(mode="after")
    def _refuse_a_wildcard_cors_origin(self) -> Settings:
        """A deployed environment must name the origins it trusts.

        ``*`` is not a relaxed setting here, it is a broken one. The application
        sends ``allow_credentials=True``, and the CORS specification forbids
        pairing that with a literal ``*`` - so Starlette echoes the requesting
        origin back instead, which means every site on the internet is allowed
        to make credentialed calls to this API on behalf of whoever is signed
        in. The fix is a list of origins, and the place to find that out is
        start-up.
        """
        if self.app_env in ("production", "staging") and "*" in self.cors_origin_list:
            raise ValueError(
                "CORS_ORIGINS must list the exact origins in "
                f"{self.app_env}; '*' cannot be combined with credentials. "
                "An empty value is allowed and means no browser origin is "
                "trusted."
            )
        return self

    @model_validator(mode="after")
    def _require_the_selected_provider_credential(self) -> Settings:
        """Refuse to start a deployed environment without the key it will need.

        Checked for the selected provider only: a deployment that uses
        Anthropic should not have to hold an OpenAI account. Failing at
        start-up rather than on the first model call turns a silent
        misconfiguration into an obvious one.
        """
        if self.app_env in ("production", "staging") and self.llm_api_key is None:
            raise ValueError(
                f"LLM_PROVIDER is {self.llm_provider!r}, so "
                f"{self.llm_provider.upper()}_API_KEY must be set in {self.app_env}."
            )
        return self

    @model_validator(mode="after")
    def _metrics_need_a_token_whenever_they_are_served(self) -> Settings:
        """There is no unauthenticated metrics mode, in any environment.

        Checked here rather than in the endpoint because the dangerous state is
        a deployment that *starts*: an exposition endpoint that answers anyone
        is the one Part 19 mistake that cannot be noticed by reading a
        dashboard. A deployment that enables metrics without a token fails to
        boot, which is noticed immediately.

        Development is not excepted. A developer who turns metrics on locally
        gets a one-line error telling them to set a token, which is cheaper
        than the habit of expecting the endpoint to be open.
        """
        if self.metrics_enabled and not self.metrics_token:
            raise ValueError(
                "METRICS_ENABLED is true, so METRICS_TOKEN must be set: "
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return self

    @model_validator(mode="after")
    def _pricing_must_parse(self) -> Settings:
        """Refuse to start with a price book nobody can read.

        A malformed book would otherwise surface as "cost unavailable" on every
        report - indistinguishable from the legitimate unpriced case, and
        therefore invisible.
        """
        self.price_book  # noqa: B018 - built for its validation, cached below
        return self

    @cached_property
    def price_book(self) -> PriceBook:
        """The configured prices, parsed once.

        Cached on the settings object, which lives as long as the application,
        so a report does not re-parse JSON per request and every figure in one
        deployment comes from one book version.
        """
        return PriceBook.from_json(self.llm_pricing)

    @property
    def llm_api_key(self) -> SecretStr | None:
        """Credential for the selected provider, if one is configured."""
        keys: dict[str, SecretStr | None] = {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
        }
        return keys[self.llm_provider]

    @property
    def llm_model(self) -> str:
        """Default model for the selected provider."""
        models = {"anthropic": self.anthropic_model, "openai": self.openai_model}
        return models[self.llm_provider]

    @property
    def allowed_models(self) -> frozenset[str]:
        """Models a request may name.

        Always includes the configured model, so the documented default works
        whatever else is listed.
        """
        extra = {name.strip() for name in self.llm_allowed_models.split(",") if name.strip()}
        return frozenset({self.llm_model, *extra})

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


class ConfigurationError(RuntimeError):
    """The process cannot start with the configuration it was given.

    Raised instead of letting a ``ValidationError`` out, because of what that
    exception *renders*: pydantic appends the input it rejected to every
    message. For a field validator that input is the field's own value, and for
    a model validator it is the whole settings dictionary - so a deployment
    that mistyped one setting prints the ones beside it, API key included, into
    the first lines of its log.

    Long values are abbreviated in the middle, which makes it worse rather than
    better: a short secret survives the abbreviation whole, so whether a
    credential is published depends on how long it happens to be.

    So this carries the validators' own messages and nothing else. Those
    messages are written to be safe - they name the setting and what it should
    look like, never the value - and the ones guarding credentials say so
    explicitly.
    """


def _describe(error: ValidationError) -> str:
    """Render a validation failure without the value that caused it.

    ``loc`` is the setting's name and ``msg`` is the validator's own sentence.
    The ``input`` key on each entry is what must not be read, and the way to
    guarantee it is not read is to build the message from the other two.
    """
    lines: list[str] = []
    for entry in error.errors():
        location = ".".join(str(part) for part in entry["loc"]) or "configuration"
        message = str(entry["msg"]).removeprefix("Value error, ")
        lines.append(f"  {location}: {message}")

    count = len(lines)
    heading = f"{count} configuration problem{'' if count == 1 else 's'}:"
    return "\n".join([heading, *lines])


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    The one place a deployment's configuration is read, and therefore the one
    place that has to be careful about what a failure says out loud.
    """
    try:
        return Settings()
    except ValidationError as error:
        # `from None`: chaining would print the original exception underneath
        # this one, and the original is precisely what carries the values.
        raise ConfigurationError(_describe(error)) from None
