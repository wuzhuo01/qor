"""Filesystem-backed controlled sandbox world.

Each resource is `{root}/{resource}.json` with `{value, applied}`. Writer and
reader are separate objects; they share no in-memory dict. The only coupling is
the directory on disk. Fault injection is per-adapter so tests can break write
or read independently.
"""

from __future__ import annotations

import json
from pathlib import Path

from qor.kernel.errors import QORError

__all__ = ["FilesystemSandboxWorldReader", "FilesystemSandboxWorldWriter"]


def _resource_path(root: Path, resource: str) -> Path:
    if not resource or any(token in resource for token in ("/", "\\", "..")):
        raise QORError(f"invalid sandbox world resource '{resource}'")
    return root / f"{resource}.json"


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return default


def _as_keys(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _load(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"value": 0, "applied": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QORError(f"sandbox world file {path} is not an object")
    return payload


class FilesystemSandboxWorldWriter:
    def __init__(self, root: Path, *, write_fault: str | None = None) -> None:
        self._root = Path(root)
        self._write_fault = write_fault

    def apply(self, *, resource: str, delta: int, idempotency_key: str) -> None:
        if self._write_fault == "noop":
            return
        self._root.mkdir(parents=True, exist_ok=True)
        path = _resource_path(self._root, resource)
        data = _load(path)
        applied = _as_keys(data.get("applied", []))
        if idempotency_key in applied:
            return
        value = _as_int(data.get("value", 0)) + delta
        applied.append(idempotency_key)
        encoded = json.dumps({"value": value, "applied": applied}, indent=2)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(encoded, encoding="utf-8")
        tmp.replace(path)


class FilesystemSandboxWorldReader:
    def __init__(self, root: Path, *, read_fault: str | None = None) -> None:
        self._root = Path(root)
        self._read_fault = read_fault

    def read(self, resource: str) -> dict[str, str] | None:
        if self._read_fault == "missing":
            return None
        target = "other" if self._read_fault == "unrelated" else resource
        path = _resource_path(self._root, target)
        if not path.exists():
            return None
        data = _load(path)
        return {"value": str(_as_int(data.get("value", 0)))}
