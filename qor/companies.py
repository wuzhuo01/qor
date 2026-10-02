"""Load a company profile from outside QOR Core.

A company is a directory ``business/<name>/`` with ``company.py``. That file
defines ``load_company_config()`` and returns a ``QORConfig``. Core does not
name any company. Adding another company is another directory with the same
function.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from qor.services.config import QORConfig

__all__ = ["load_company"]


def load_company(name: str, *, root: Path | None = None) -> QORConfig:
    if not name.isidentifier():
        raise ValueError(f"unknown company '{name}'")
    base = root or Path.cwd()
    file = base / "business" / name / "company.py"
    if not file.is_file():
        raise ValueError(f"unknown company '{name}'")
    module_name = f"_qor_company_{name}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ValueError(f"unknown company '{name}'")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(base))
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] == str(base):
            sys.path.pop(0)
    load = getattr(module, "load_company_config", None)
    if not callable(load):
        raise ValueError(f"company '{name}' does not define load_company_config()")
    config = load()
    if not isinstance(config, QORConfig):
        raise ValueError(f"company '{name}' did not return a QORConfig")
    return config
