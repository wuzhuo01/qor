"""ScriptedLLM -- a test double that behaves like a real model reply.

This exists so the ``LLMPlanner`` parsing, validation and repair path is exercised in
CI without network access. It returns realistic model output: markdown fences,
occasional malformed first attempts, ordering that differs from the stub planner.

It is explicitly **not** evidence that the LLM planner works against a real
provider. That claim requires credentials and is reported as an open Mock boundary.
"""

from __future__ import annotations

from collections.abc import Iterable

from qor.ports.llm import LLMUnavailable

__all__ = ["ScriptedLLM"]


class ScriptedLLM:
    def __init__(self, replies: Iterable[str], *, name: str = "scripted") -> None:
        self._replies = list(replies)
        self._name = name
        self.calls: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return f"scripted:{self._name}"

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int = 2048,
    ) -> str:
        self.calls.append((system, user))
        if not self._replies:
            raise LLMUnavailable(f"{self.name} has no scripted replies left")
        return self._replies.pop(0)
