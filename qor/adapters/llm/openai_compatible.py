"""OpenAI-compatible chat completions client.

Uses stdlib ``urllib`` so QOR takes no vendor SDK dependency: the LLM is an adapter
detail, and a vendor package in the dependency tree has a habit of leaking upward.

Raises ``LLMUnavailable`` when unconfigured rather than falling back to anything.
A run that silently used a canned plan while claiming to be AI-planned would put a
false statement in the audit log, which is worse than failing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime

from qor.adapters.persistence.llm_usage import LlmUsageRecord
from qor.ports.llm import LLMUnavailable

__all__ = ["OpenAICompatibleLLM", "from_environment", "provider_configured"]

ENV_BASE_URL = "QOR_LLM_BASE_URL"
ENV_API_KEY = "QOR_LLM_API_KEY"
ENV_MODEL = "QOR_LLM_MODEL"


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self.calls: list[dict[str, object]] = []
        self._usage: list[LlmUsageRecord] | None = None

    def bind_usage(self, records: list[LlmUsageRecord]) -> None:
        """Append provider usage records onto a list that outlives this client."""
        self._usage = records

    @property
    def name(self) -> str:
        return f"openai-compatible:{self._model}"

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int = 2048,
        task_ref: str = "",
    ) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": f"{system}\n\nJSON shape:\n{schema_hint}"},
                {"role": "user", "content": user},
            ],
            # Low but non-zero: planning benefits from some variety, and a planner
            # that returns byte-identical output every time is hard to distinguish
            # from a lookup table.
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        endpoint = f"{self._base_url}/chat/completions"
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise LLMUnavailable(f"{self.name} timed out after {self._timeout}s: {exc}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"{self.name} request failed: {exc}") from exc

        self.calls.append(
            {
                "endpoint": endpoint,
                "model": self._model,
                "max_tokens": max_tokens,
            }
        )

        try:
            payload = json.loads(raw)
            choice = payload["choices"][0]
            finish = choice.get("finish_reason")
            content = choice["message"].get("content")
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"{self.name} returned non-JSON: {exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"{self.name} returned an unexpected payload shape") from exc

        content_text = content if isinstance(content, str) else ""
        self.calls[-1]["finish_reason"] = finish
        self.calls[-1]["content_length"] = len(content_text)
        if not content_text.strip():
            raise LLMUnavailable(
                f"{self.name} returned empty content (finish_reason={finish}); "
                "reasoning models need a higher max_tokens budget so the JSON is not "
                "consumed by chain-of-thought"
            )
        input_tokens, output_tokens = _provider_tokens(payload)
        if input_tokens is None or output_tokens is None:
            raise LLMUnavailable(f"{self.name} response has no provider usage token counts")
        self.calls[-1]["task_ref"] = task_ref
        self.calls[-1]["input_tokens"] = input_tokens
        self.calls[-1]["output_tokens"] = output_tokens
        record = LlmUsageRecord(
            task_ref=task_ref,
            model=self._model,
            occurred_at=datetime.now(UTC),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if self._usage is not None:
            self._usage.append(record)
        return content_text


def _provider_tokens(payload: object) -> tuple[int | None, int | None]:
    """Read token counts from provider usage. Never from character length."""
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _as_token(usage.get("prompt_tokens", usage.get("input_tokens"))), _as_token(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )


def _as_token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def provider_configured() -> bool:
    """True when the environment can actually reach a real model."""
    return all(os.getenv(name) for name in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL))


def from_environment() -> OpenAICompatibleLLM:
    """Build a client from environment variables, or explain what is missing."""
    missing = [name for name in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL) if not os.getenv(name)]
    if missing:
        raise LLMUnavailable(
            f"no LLM provider configured; set {', '.join(missing)} to enable the LLM planner"
        )
    return OpenAICompatibleLLM(
        base_url=os.environ[ENV_BASE_URL],
        api_key=os.environ[ENV_API_KEY],
        model=os.environ[ENV_MODEL],
    )
