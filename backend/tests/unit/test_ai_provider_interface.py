"""The provider interface and the AI error hierarchy.

Includes the architectural assertion the abstraction exists for: no module
outside ``app/ai/providers/`` may import a provider SDK.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import subprocess
import sys

import pytest
from pydantic import BaseModel

from app.ai.exceptions import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.ai.models import LLMMessage, LLMRequest, LLMResponse, LLMStructuredResponse, LLMUsage
from app.ai.providers.base import LLMProvider
from app.core.exceptions import AppError

AI_EXCEPTIONS = [
    LLMConfigurationError,
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMProviderError,
    LLMInvalidResponseError,
]

APP_ROOT = pathlib.Path(inspect.getfile(LLMProvider)).parents[3]
PROVIDER_SDKS = ("anthropic", "openai")


class Reply(BaseModel):
    answer: str


class StubProvider(LLMProvider):
    """A complete implementation, so the interface can be exercised without a
    vendor. Returns a fixed answer; it makes no network call."""

    name = "stub"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=f"stubbed: {request.messages[-1].content}",
            provider=self.name,
            model=request.model,
            usage=LLMUsage(input_tokens=1, output_tokens=2),
            latency_ms=0.0,
        )

    async def generate_structured[DataT: BaseModel](
        self, request: LLMRequest, schema: type[DataT]
    ) -> LLMStructuredResponse[DataT]:
        response = await self.generate(request)
        return LLMStructuredResponse[schema](  # type: ignore[valid-type]
            data=schema.model_validate({"answer": response.content}),
            response=response,
        )


def a_request() -> LLMRequest:
    return LLMRequest(messages=[LLMMessage.user("ping")], model="stub-model")


# -- The interface ------------------------------------------------------------


def test_the_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_both_methods_are_abstract() -> None:
    assert LLMProvider.__abstractmethods__ == {"generate", "generate_structured"}


def test_a_partial_implementation_cannot_be_instantiated() -> None:
    class HalfProvider(LLMProvider):
        name = "half"

        async def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(TypeError):
        HalfProvider()  # type: ignore[abstract]


def test_an_adapter_must_name_itself() -> None:
    """The name reaches LLMResponse.provider and the logs; without it a call
    could not be attributed to a vendor."""
    with pytest.raises(TypeError, match="name"):

        class Nameless(LLMProvider):
            async def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
                raise NotImplementedError

            async def generate_structured[DataT: BaseModel](  # pragma: no cover
                self, request: LLMRequest, schema: type[DataT]
            ) -> LLMStructuredResponse[DataT]:
                raise NotImplementedError


def test_the_interface_is_asynchronous() -> None:
    """Model calls are I/O bound and must not block the event loop."""
    assert inspect.iscoroutinefunction(LLMProvider.generate)
    assert inspect.iscoroutinefunction(LLMProvider.generate_structured)


async def test_a_complete_implementation_satisfies_the_contract() -> None:
    response = await StubProvider().generate(a_request())

    assert isinstance(response, LLMResponse)
    assert response.provider == "stub"
    assert response.usage.total_tokens == 3


async def test_structured_generation_returns_a_validated_model() -> None:
    structured = await StubProvider().generate_structured(a_request(), Reply)

    assert isinstance(structured.data, Reply)
    assert structured.data.answer == "stubbed: ping"
    assert structured.response.provider == "stub"


# -- Exceptions ---------------------------------------------------------------


@pytest.mark.parametrize("error", AI_EXCEPTIONS)
def test_every_ai_error_is_an_llm_error(error: type[LLMError]) -> None:
    """One class to catch for "the model call failed"."""
    assert issubclass(error, LLMError)


@pytest.mark.parametrize("error", [LLMError, *AI_EXCEPTIONS])
def test_every_ai_error_reaches_the_application_error_envelope(error: type[LLMError]) -> None:
    """So a failure renders with a correlation id instead of an unhandled 500."""
    assert issubclass(error, AppError)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (LLMError, 502, "llm_error"),
        (LLMConfigurationError, 500, "llm_configuration_error"),
        (LLMAuthenticationError, 500, "llm_authentication_error"),
        (LLMRateLimitError, 429, "llm_rate_limited"),
        (LLMTimeoutError, 504, "llm_timeout"),
        (LLMProviderError, 502, "llm_provider_error"),
        (LLMInvalidResponseError, 502, "llm_invalid_response"),
    ],
)
def test_error_status_codes_and_codes(error: type[LLMError], status_code: int, code: str) -> None:
    assert error.status_code == status_code
    assert error.code == code


def test_a_provider_rejecting_our_key_is_not_a_401() -> None:
    """401 in this application means the caller's access token is bad. A
    provider refusing *our* key is a deployment fault, and returning 401 would
    send a properly signed-in user to the login screen."""
    assert LLMAuthenticationError.status_code == 500
    assert LLMAuthenticationError.status_code != 401


def test_an_ai_error_is_not_confused_with_a_client_error() -> None:
    """None of these blame the caller: a 4xx would tell a client to change its
    request when the problem is upstream. Rate limiting is the one exception,
    because retrying later is exactly the right response."""
    for error in [LLMError, *AI_EXCEPTIONS]:
        if error is LLMRateLimitError:
            continue
        assert error.status_code >= 500, error.__name__


@pytest.mark.parametrize("error", [LLMError, *AI_EXCEPTIONS])
def test_default_messages_do_not_quote_a_provider(error: type[LLMError]) -> None:
    """AppError messages are returned to clients, so the defaults must not
    name a vendor, model, quota or endpoint."""
    message = error.message.lower()

    assert message
    for sdk in PROVIDER_SDKS:
        assert sdk not in message


def test_the_provider_name_is_recorded_but_not_returned_to_clients() -> None:
    """Which vendor served a request is internal architecture: useful in a log
    line, not something an API response should disclose."""
    error = LLMTimeoutError(provider="some-provider")

    assert error.provider == "some-provider"
    assert "some-provider" not in str(error.details)
    assert error.details == {}
    # Still visible where it is useful.
    assert "some-provider" in str(error)


def test_an_error_without_a_provider_reads_cleanly() -> None:
    assert str(LLMTimeoutError()) == LLMTimeoutError.message


def test_a_custom_message_overrides_the_default() -> None:
    assert LLMProviderError("model overloaded").message == "model overloaded"


def test_rate_limiting_can_carry_a_retry_hint() -> None:
    """So a caller can back off by the stated amount instead of guessing."""
    assert LLMRateLimitError(retry_after_seconds=30.0).retry_after_seconds == 30.0
    assert LLMRateLimitError().retry_after_seconds is None


def test_ai_errors_are_catchable_as_ordinary_exceptions() -> None:
    with pytest.raises(LLMError):
        raise LLMTimeoutError(provider="some-provider")


def test_the_original_cause_can_be_chained() -> None:
    """Adapters translate SDK exceptions; the cause must survive for the log
    even though the SDK type does not escape."""
    original = ValueError("sdk specific failure")

    try:
        try:
            raise original
        except ValueError as exc:
            raise LLMProviderError("upstream failed", provider="some-provider") from exc
    except LLMProviderError as exc:
        assert exc.__cause__ is original


# -- The architectural rule ---------------------------------------------------


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Top-level package names imported by a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_application_module_outside_the_adapters_imports_a_provider_sdk() -> None:
    """The reason this abstraction exists.

    Adapters under app/ai/providers/ may import their vendor's SDK. Everywhere
    else must go through LLMProvider, so that changing or adding a provider is
    a change in one package rather than across the codebase.
    """
    adapters = APP_ROOT / "app" / "ai" / "providers"
    offenders: list[str] = []

    for path in (APP_ROOT / "app").rglob("*.py"):
        if adapters in path.parents:
            continue
        leaked = _imported_modules(path) & set(PROVIDER_SDKS)
        if leaked:
            offenders.append(f"{path.relative_to(APP_ROOT)} imports {sorted(leaked)}")

    assert not offenders, "provider SDK imported outside the adapters: " + "; ".join(offenders)


@pytest.mark.parametrize("layer", ["api", "services", "core", "repositories", "models"])
def test_the_application_layers_import_no_provider_sdk(layer: str) -> None:
    """Named explicitly, because these are the layers a reviewer will ask
    about: the endpoint, the service and the configuration must all reach a
    model only through the gateway.
    """
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in (APP_ROOT / "app" / layer).rglob("*.py")
        if _imported_modules(path) & set(PROVIDER_SDKS)
    ]

    assert not offenders, f"app/{layer} imports a provider SDK: {offenders}"


def test_the_core_ai_modules_import_no_sdk_at_all() -> None:
    """Stronger, and true for this stage: no adapter exists yet."""
    for module in ("app.ai.models", "app.ai.exceptions", "app.ai.providers.base"):
        assert module in sys.modules
        leaked = _imported_modules(pathlib.Path(sys.modules[module].__file__ or "")) & set(
            PROVIDER_SDKS
        )
        assert not leaked, f"{module} imports {leaked}"


def test_no_provider_sdk_is_loaded_by_importing_the_package() -> None:
    """Importing app.ai must not drag a vendor client into the process.

    Code that needs only the shared types should not pay for two SDKs it will
    not call; the adapters live under app.ai.providers for that reason.

    Run in a fresh interpreter on purpose: by the time this test executes, its
    own sibling modules have already imported both SDKs, so inspecting this
    process's sys.modules would prove nothing.
    """
    probe = (
        "import sys, app.ai; "
        "print(sorted(n for n in sys.modules "
        f"if n.split('.')[0] in {PROVIDER_SDKS!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=APP_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", f"app.ai imported a provider SDK: {result.stdout}"


def test_the_adapters_do_import_their_sdks() -> None:
    """The other half: the boundary is a boundary, not a ban."""
    import app.ai.providers.registry  # noqa: F401

    assert {name for name in sys.modules if name.split(".")[0] in PROVIDER_SDKS}


def test_the_shared_types_carry_no_sdk_objects() -> None:
    """Field annotations are plain Python and Pydantic types only."""
    for model in (LLMMessage, LLMRequest, LLMResponse, LLMUsage):
        for field in model.model_fields.values():
            annotation = str(field.annotation).lower()
            for sdk in PROVIDER_SDKS:
                assert sdk not in annotation, f"{model.__name__}: {annotation}"
