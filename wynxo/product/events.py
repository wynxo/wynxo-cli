"""Typed product events used by the terminal presentation layer.

The agent still owns execution. These events are the small, stable vocabulary
the product UI consumes so rendering does not need to infer state from strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time


@dataclass(frozen=True)
class ProductEvent:
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class StageEvent(ProductEvent):
    name: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ToolStarted(ProductEvent):
    name: str = ""
    summary: str = ""


@dataclass(frozen=True)
class ToolFinished(ProductEvent):
    name: str = ""
    ok: bool = True
    display: str = ""


@dataclass(frozen=True)
class TurnFinished(ProductEvent):
    files: int = 0
    additions: int = 0
    deletions: int = 0
    tool_calls: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    elapsed: float = 0.0


class EventBuffer:
    """Bounded recent product events for diagnostics and UI summaries."""

    def __init__(self, limit: int = 256) -> None:
        self.limit = max(16, limit)
        self._items: list[ProductEvent] = []

    def emit(self, event: ProductEvent) -> None:
        self._items.append(event)
        if len(self._items) > self.limit:
            del self._items[:-self.limit]

    def recent(self, limit: int = 40) -> list[ProductEvent]:
        return self._items[-max(0, limit):]

    def clear(self) -> None:
        self._items.clear()
