"""LLM port.

Kept minimal on purpose: a single "complete this into JSON" call. QOR's planning
logic must not become prompt-chaining, so anything more elaborate belongs in an
adapter, not in this interface.
"""

from __future__ import annotations

from typing import Protocol

from qor.kernel.errors import QORError

__all__ = ["LLMPort", "LLMUnavailable"]


class LLMUnavailable(QORError):
    """No provider is configured or reachable.

    Raised rather than silently falling back to a stub: a run that quietly used a
    hardcoded plan while claiming to be AI-planned would be a lie in the audit log.
    """


class LLMPort(Protocol):
    @property
    def name(self) -> str:
        """Provider identity, recorded on the plan so the audit log says who planned."""
        ...

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int = 2048,
        task_ref: str = "",
    ) -> str:
        """Return a JSON string. Parsing and validation are the caller's job."""
        ...
