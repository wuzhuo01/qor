"""Scheduler -- decide whether a Schedule should open a Task now.

It calls Intake and stops. It does not plan, assign, review, or act.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from qor.domain.enums import ScheduleCadence, ScheduleTickResult
from qor.domain.schedule import TERMINAL_TASK_STATUSES, Schedule
from qor.kernel.actor import ActorRef
from qor.services.intake import IntakeService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["SchedulerService", "TickResult"]


class TickResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_ref: str
    occurrence_key: str
    task_ref: str
    result: ScheduleTickResult


class SchedulerService:
    def __init__(self, runtime: Runtime, engine: TransitionEngine, intake: IntakeService) -> None:
        self._rt = runtime
        self._engine = engine
        self._intake = intake
        self._actor = ActorRef.system("QOR-SCHEDULER")

    def register(
        self,
        *,
        objective: str,
        cadence: ScheduleCadence,
        next_run_at: datetime,
        run_at: datetime | None = None,
        interval: timedelta | None = None,
    ) -> Schedule:
        now = self._rt.clock.now()
        sid, ref = self._rt.mint("SCHED")
        schedule = Schedule(
            id=sid,
            ref=ref,
            status="ENABLED",
            created_at=now,
            created_by=self._actor,
            updated_at=now,
            objective=objective,
            cadence=cadence,
            run_at=run_at if cadence is ScheduleCadence.ONCE else None,
            interval=interval if cadence is ScheduleCadence.EVERY else None,
            next_run_at=run_at if cadence is ScheduleCadence.ONCE else next_run_at,
        )
        schedule = self._rt.repos.schedules.insert(schedule)
        self._engine.record_creation(
            schedule,
            actor=self._actor,
            reason_code="SCHEDULE_DUE",
            reason=f"registered {schedule.cadence} schedule {schedule.ref}",
            correlation_id=schedule.id,
            event_name="qor.schedule.registered",
        )
        return schedule

    def disable(self, schedule_id: UUID) -> Schedule:
        schedule = self._rt.repos.schedules.get(schedule_id)
        if schedule.status == "DISABLED":
            return schedule
        result = self._engine.fire(
            schedule,
            "disable",
            actor=self._actor,
            reason_code="SCHEDULE_DISABLED",
            reason=f"disabled {schedule.ref}",
            correlation_id=schedule.id,
        )
        return self._rt.repos.schedules.save(result.entity)

    def tick(self, schedule_id: UUID, now: datetime) -> TickResult:
        schedule = self._rt.repos.schedules.get(schedule_id)
        key = schedule.occurrence_key()
        if now < schedule.next_run_at:
            return TickResult(
                schedule_ref=schedule.ref,
                occurrence_key=key,
                task_ref="",
                result=ScheduleTickResult.NOT_DUE,
            )
        if schedule.last_occurrence_key == key:
            return self._record(schedule, key, "", ScheduleTickResult.SKIPPED_DUPLICATE)
        if schedule.status == "DISABLED":
            return self._record(schedule, key, "", ScheduleTickResult.SKIPPED_DISABLED)
        if self._has_active_task(schedule):
            return self._record(schedule, key, "", ScheduleTickResult.SKIPPED_ACTIVE)

        task = self._intake.open_from_schedule(schedule)
        schedule = self._rt.repos.schedules.get(schedule.id)
        update: dict[str, object] = {
            "last_occurrence_key": key,
            "last_task_id": task.id,
            "updated_at": now,
        }
        if schedule.cadence is ScheduleCadence.EVERY:
            assert schedule.interval is not None
            update["next_run_at"] = schedule.next_run_at + schedule.interval
        schedule = self._rt.repos.schedules.save(schedule.model_copy(update=update))
        if schedule.cadence is ScheduleCadence.ONCE:
            fired = self._engine.fire(
                schedule,
                "disable",
                actor=self._actor,
                reason_code="SCHEDULE_DISABLED",
                reason=f"{schedule.ref} completed its single occurrence",
                correlation_id=schedule.id,
            )
            schedule = self._rt.repos.schedules.save(fired.entity)
        return self._record(schedule, key, task.ref, ScheduleTickResult.CREATED)

    def _has_active_task(self, schedule: Schedule) -> bool:
        if schedule.last_task_id is None:
            return False
        task = self._rt.repos.tasks.find(schedule.last_task_id)
        return task is not None and task.status not in TERMINAL_TASK_STATUSES

    def _record(
        self,
        schedule: Schedule,
        occurrence_key: str,
        task_ref: str,
        result: ScheduleTickResult,
    ) -> TickResult:
        self._engine.emit(
            "qor.schedule.triggered",
            entity=schedule,
            actor=self._actor,
            payload={
                "schedule_ref": schedule.ref,
                "occurrence_key": occurrence_key,
                "task_ref": task_ref,
                "result": result.value,
            },
            correlation_id=schedule.id,
        )
        return TickResult(
            schedule_ref=schedule.ref,
            occurrence_key=occurrence_key,
            task_ref=task_ref,
            result=result,
        )
