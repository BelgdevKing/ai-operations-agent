"""Where tools are found.

One lookup, by name, with no branch on the name anywhere else in the
application. That is the whole value of it: adding a tool means registering a
class, not editing a chain of comparisons that some caller will forget.

In memory and static. The `tools` table exists in the schema but carries no
input schema and nothing writes to it, so pointing this at the database would
mean a migration in aid of configuration nothing yet needs. Swapping the
storage later changes where `register` is called from, not what `resolve` means.

**Part 13 registers no real tools.** A production registry is empty, on
purpose: business tools arrive in the next part, and an empty registry is the
honest representation of that.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.tools.base import AnyTool
from app.tools.exceptions import ToolNotFoundError, ToolRegistrationError
from app.tools.models import ToolMetadata


class ToolRegistry:
    """The tools this deployment has."""

    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AnyTool) -> None:
        """Add a tool.

        Raises:
            ToolRegistrationError: Something is already registered under that
                name. Refused rather than replaced - a silent overwrite is how
                a deployment ends up running a tool nobody thinks is installed.
        """
        name = tool.metadata.name

        existing = self._tools.get(name)
        if existing is not None:
            raise ToolRegistrationError(
                f"Two tools are registered as {name!r}: "
                f"{type(existing).__name__} and {type(tool).__name__}."
            )

        self._tools[name] = tool

    def resolve(self, name: str) -> AnyTool:
        """The tool registered under *name*.

        Raises:
            ToolNotFoundError: Nothing is registered under it. Expected rather
                than exceptional - the name came from a model.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError()
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def describe(self) -> list[ToolMetadata]:
        """What is registered, in name order.

        Metadata only. There is no path from here to a tool's implementation,
        its configuration or anything it holds - which is what makes this safe
        to expose over an API.
        """
        return [self._tools[name].metadata for name in sorted(self._tools)]

    def enabled(self) -> list[ToolMetadata]:
        """Only the tools that are switched on."""
        return [metadata for metadata in self.describe() if metadata.enabled]

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry({len(self._tools)} tools)"
