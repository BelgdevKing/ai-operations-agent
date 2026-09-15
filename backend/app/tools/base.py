"""The interface every tool implements.

A tool is a class, not a callable. That is the point: a bare function could be
registered with no schema, no safety classification and no deadline, and the
framework would have nothing to enforce. Declaring those is the price of
becoming a tool, and `__init_subclass__` collects it at import time rather than
on the first call.

A tool implementation is responsible for its own business behaviour and
nothing else. Validation, timeouts, cancellation, authorization, error
normalisation and logging all happen around it in the executor - so a tool body
is free to be the simple thing it should be.
"""

from __future__ import annotations

import abc
from typing import Any

from pydantic import BaseModel

from app.tools.exceptions import ToolRegistrationError
from app.tools.models import RESERVED_ARGUMENT_NAMES, ToolExecutionContext, ToolMetadata


class Tool[InputT: BaseModel, OutputT: BaseModel](abc.ABC):
    """One capability an agent may invoke.

    Subclasses set three class attributes and implement one method::

        class LookUpThing(Tool[ThingQuery, ThingResult]):
            metadata = ToolMetadata(name="look_up_thing", ...)
            input_model = ThingQuery
            output_model = ThingResult

            async def execute(self, arguments, context): ...
    """

    metadata: ToolMetadata
    """What the tool is. Read by the registry and by policy; never by the tool."""

    input_model: type[InputT]
    """Schema the arguments are validated against before `execute` is reached."""

    output_model: type[OutputT]
    """Schema the return value is validated against before anyone sees it."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)

        # Abstract intermediates are allowed to be incomplete; concrete tools
        # are not, and the failure belongs at import time.
        if getattr(cls, "__abstract__", False) or abc.ABC in cls.__bases__:
            return

        for attribute in ("metadata", "input_model", "output_model"):
            if getattr(cls, attribute, None) is None:
                raise ToolRegistrationError(
                    f"{cls.__name__} must set a '{attribute}' class attribute."
                )

        _reject_reserved_fields(cls)
        _check_approval_summary(cls)

    @abc.abstractmethod
    async def execute(self, arguments: InputT, context: ToolExecutionContext) -> OutputT:
        """Do the work.

        Args:
            arguments: Already validated against `input_model`. Untrusted in
                origin but well-formed by the time it arrives.
            context: The verified identity this execution acts for. **Read
                organization and user from here**, never from `arguments`.

        Returns:
            An instance of `output_model`. Anything else is refused by the
            executor and never reaches the agent runtime.

        Raises:
            ToolError: Where the tool wants to be specific about a failure.
                Anything else it raises is caught and normalised, with the
                original logged and a generic message returned.
        """


AnyTool = Tool[Any, Any]
"""A tool with its schemas erased, for the places that hold tools generically."""


def _reject_reserved_fields(tool: type[Tool[Any, Any]]) -> None:
    """Refuse a tool whose input schema asks for identity.

    The executor rejects reserved argument *names* at call time regardless.
    This catches the same mistake one layer earlier - when the tool is written -
    because a tool that declares `organization_id` has already been designed to
    take its tenant from whoever called it.
    """
    declared = set(tool.input_model.model_fields)
    reserved = declared & RESERVED_ARGUMENT_NAMES

    if reserved:
        names = ", ".join(sorted(reserved))
        raise ToolRegistrationError(
            f"{tool.__name__} declares reserved argument(s): {names}. "
            "Identity comes from the execution context, not from arguments."
        )


def _check_approval_summary(tool: type[Tool[Any, Any]]) -> None:
    """Refuse a tool whose approval summary names an argument it does not take.

    The summary is an allow-list read at approval time, when nothing is left to
    validate it against - a misspelt field would simply render as absent, and
    the reviewer would see a smaller summary than the author intended with
    nothing anywhere saying why. Import time is where that is still a typo.
    """
    summary = tool.metadata.approval_summary
    if summary is None:
        return

    declared = set(tool.input_model.model_fields)
    unknown = [name for name in summary.named_fields if name not in declared]

    if unknown:
        names = ", ".join(sorted(unknown))
        raise ToolRegistrationError(
            f"{tool.__name__} declares an approval summary over unknown "
            f"argument(s): {names}. A summary may only name fields of "
            f"{tool.input_model.__name__}."
        )
