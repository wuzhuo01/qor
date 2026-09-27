"""Identifiers.

Every entity carries two identities:

* ``id``  -- UUIDv7, time-ordered, machine primary key.
* ``ref`` -- human readable, e.g. ``TASK-20260912-001``. Used in logs and reports.

UUIDv7 is generated locally because ``uuid.uuid7`` is not available on Python 3.11.
Layout follows RFC 9562 section 5.7.
"""

from __future__ import annotations

import os
import threading
from datetime import date
from uuid import UUID

__all__ = ["RefMinter", "new_id"]


def new_id(*, at_ms: int | None = None) -> UUID:
    """Return a UUIDv7.

    ``at_ms`` allows deterministic tests to pin the timestamp component.
    """
    if at_ms is None:
        import time

        at_ms = int(time.time() * 1000)

    rand = os.urandom(10)

    # 48 bits: milliseconds since epoch
    ts = at_ms & 0xFFFF_FFFF_FFFF
    # 12 bits of randomness in rand_a
    rand_a = int.from_bytes(rand[:2], "big") & 0x0FFF
    # 62 bits of randomness in rand_b
    rand_b = int.from_bytes(rand[2:10], "big") & 0x3FFF_FFFF_FFFF_FFFF

    value = ts << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 9562 variant
    value |= rand_b
    return UUID(int=value)


class RefMinter:
    """Mints human readable refs such as ``TASK-20260912-001``.

    Sequence is per (prefix, day). Thread-safe because agents run concurrently.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def mint(self, prefix: str, *, on: date) -> str:
        day = on.strftime("%Y%m%d")
        key = (prefix, day)
        with self._lock:
            nxt = self._counters.get(key, 0) + 1
            self._counters[key] = nxt
        return f"{prefix}-{day}-{nxt:03d}"
