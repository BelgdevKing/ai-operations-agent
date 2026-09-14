"""The contract every provider adapter implements.

An adapter's whole job is translation: take an :class:`LLMRequest`, speak
whatever dialect its vendor expects, and return an :class:`LLMResponse`. It
raises only ``app.ai.exceptions`` errors, so no SDK exception type reaches the
gateway above it.

No implementation lives here. Concrete adapters and the SDK dependencies they
need arrive in a later stage; this module exists so the layers above can be
written and tested against the interface rather than against a vendor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from app.ai.models import LLMRequest, LLMResponse, LLMStructuredResponse


class LLMProvider(ABC):
    """A source of model completions.

    Subclasses set :attr:`name` and implement both methods:

        class SomeProvider(LLMProvider):
            name = "some-provider"

            async def generate(self, request: LLMRequest) -> LLMResponse: ...

    Implementations are expected to be safe for concurrent use: the gateway
    holds one instance per provider for the process lifetime rather than
    building one per request, because most SDK clients pool connections and
    recreating them per call is wasteful.
    """

    name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # The name ends up in LLMResponse.provider and in log lines, so an
        # adapter that forgets it would produce unattributable records. Catch
        # that at import rather than at the first call.
        if not getattr(cls, "__abstract__", False) and not getattr(cls, "name", None):
            raise TypeError(f"{cls.__name__} must set a 'name' class attribute")

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Produce a completion.

        Args:
            request: What to ask, already provider-independent.

        Returns:
            The completion, with usage and latency filled in.

        Raises:
            LLMError: Any failure, as one of the subclasses in
                ``app.ai.exceptions``. Provider SDK exceptions must be caught
                and translated, never allowed to propagate.
        """

    @abstractmethod
    async def generate_structured[DataT: BaseModel](
        self,
        request: LLMRequest,
        schema: type[DataT],
    ) -> LLMStructuredResponse[DataT]:
        """Produce a completion parsed into *schema*.

        The implementation is responsible for getting the model to emit
        something matching the schema - through a native structured-output
        feature where the provider has one, or by instructing and parsing where
        it does not - and for validating the result before returning it. A
        caller receives either a valid ``schema`` instance or an exception,
        never an unchecked dictionary.

        Args:
            request: What to ask.
            schema: Pydantic model the output must satisfy.

        Returns:
            The validated value together with the underlying response, so usage
            and latency are not lost.

        Raises:
            LLMInvalidResponseError: The output could not be parsed or did not
                validate against *schema*.
            LLMError: Any other failure.
        """
