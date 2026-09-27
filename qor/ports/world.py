"""Ports for a controlled sandbox world.

The world is outside Action status. The writer mutates it; the reader rereads it.
Verification must use the reader, never the writer's return value.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["WorldReader", "WorldWriter"]


class WorldWriter(Protocol):
    def apply(self, *, resource: str, delta: int, idempotency_key: str) -> None: ...


class WorldReader(Protocol):
    def read(self, resource: str) -> dict[str, str] | None: ...
