"""Cooperative cancellation for a run.

A token the runtime checks between steps rather than anything that interrupts
work in flight. That is enough for the only thing worth stopping here - the
next model call - and it keeps cancellation testable: a test sets the flag and
the runtime observes it, with no sleeping and no race to lose.
"""

from __future__ import annotations

from typing import Protocol


class CancellationToken(Protocol):
    """Anything that can say whether a run should stop."""

    @property
    def cancelled(self) -> bool: ...


class Cancellation:
    """A token something else can trip."""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


class _NeverCancelled:
    """The default: a run nobody asked to stop."""

    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NEVER_CANCELLED"


NEVER_CANCELLED: CancellationToken = _NeverCancelled()
"""Shared no-op token, so callers that never cancel allocate nothing."""
