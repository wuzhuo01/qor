"""One provider-metered model call.

Token counts come from the provider usage object. Character length is not a
token count and is not stored here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

__all__ = ["LlmUsageRecord"]


class LlmUsageRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ref: str
    model: str
    occurred_at: datetime
    input_tokens: int
    output_tokens: int


def usage_locator(record: LlmUsageRecord) -> str:
    """Address of one provider call. EvidenceItem.locator uses this string."""
    return f"llm-usage://{record.task_ref}/{record.model}/{record.occurred_at.isoformat()}"
