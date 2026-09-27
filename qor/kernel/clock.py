"""Clock port.

Time is injected so that acceptance runs are reproducible: the same inputs must
produce the same StateTransition sequence on every run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FrozenClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Monotonic fake clock. Each read advances by ``step`` so ordering is stable."""

    def __init__(
        self,
        start: datetime | None = None,
        step: timedelta = timedelta(seconds=1),
    ) -> None:
        self._now = start or datetime(2026, 9, 12, 8, 0, 0, tzinfo=UTC)
        self._step = step

    def now(self) -> datetime:
        current = self._now
        self._now = self._now + self._step
        return current

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta
