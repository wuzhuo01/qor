"""Schedule -- a rule for when to open a Task. Not a Task, plan, or agent."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Self
from uuid import UUID

from pydantic import model_validator

from qor.domain.base import Entity
from qor.domain.enums import ScheduleCadence
from qor.kernel.errors import InvariantViolation

__all__ = ["TERMINAL_TASK_STATUSES", "Schedule"]

TERMINAL_TASK_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class Schedule(Entity):
    ENTITY_TYPE: ClassVar[str] = "Schedule"
    REF_PREFIX: ClassVar[str] = "SCHED"

    objective: str
    cadence: ScheduleCadence
    run_at: datetime | None = None
    interval: timedelta | None = None
    next_run_at: datetime
    last_occurrence_key: str = ""
    last_task_id: UUID | None = None

    @property
    def enabled(self) -> bool:
        return self.status == "ENABLED"

    def occurrence_key(self) -> str:
        return f"{self.ref}+{self.next_run_at.isoformat()}"

    @model_validator(mode="after")
    def _rule_is_executable(self) -> Self:
        if not self.objective.strip():
            raise InvariantViolation("I-SCH1", "Schedule.objective must be non-empty")
        if self.cadence is ScheduleCadence.ONCE:
            if self.run_at is None:
                raise InvariantViolation("I-SCH1", "ONCE schedule requires run_at")
            if self.interval is not None:
                raise InvariantViolation("I-SCH1", "ONCE schedule must not set interval")
            if self.next_run_at != self.run_at:
                raise InvariantViolation("I-SCH1", "ONCE next_run_at must equal run_at")
        elif self.cadence is ScheduleCadence.EVERY:
            if self.interval is None or self.interval <= timedelta(0):
                raise InvariantViolation("I-SCH1", "EVERY schedule requires a positive interval")
            if self.run_at is not None:
                raise InvariantViolation("I-SCH1", "EVERY schedule must not set run_at")
        return self
