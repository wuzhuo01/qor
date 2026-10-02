"""The one objective a state file is running.

This is startup metadata for the CLI. It is not a task state and not a new
organization object.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["RunRecord"]


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str
    task_id: UUID
    status: str
