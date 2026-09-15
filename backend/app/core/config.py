"""Application settings, loaded from environment variables.

Every setting has a development-friendly default so the application, the test
suite and the tooling all start without a populated environment. Production
values are supplied by the environment; nothing secret is ever hard-coded.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production", "test"]
LogFormat = Literal["json", "console"]

# Only HMAC algorithms are accepted. Restricting the set is what prevents an
# algorithm-confusion attack, where a token claims "alg": "none" or an
# asymmetric algorithm that turns the public key into a signing key.
JWTAlgorithm = Literal["HS256", "HS384", "HS512"]

# Deliberately obvious. Never use this outside local development.
DEV_JWT_SECRET_KEY = "dev-only-insecure-jwt-secret-change-me"

# Hard ceilings on the agent runtime. A deployment may be stricter than these;
# it may not be more permissive, so a misconfigured environment variable cannot
# turn one request into an unbounded spend. Mirrored in app/agents/models.py,
# which cannot import this module without a cycle.
MAX_AGENT_STEPS = 32
MAX_AGENT_OUTPUT_TOKENS = 8_192

# A tool may not be given an unbounded deadline by configuration either.
MAX_TOOL_TIMEOUT_SECONDS = 300.0

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

# Which vendor serves model calls. Only the selected one needs a credential.
LLMProviderName = Literal["anthropic", "openai"]

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
        description="Per-call ceiling passed to the provider SDK, so a model "
        "call can never hang a request indefinitely.",
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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
