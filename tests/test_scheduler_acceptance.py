"""QOR Scheduler v0.1. T1–T7. T8 is not covered.

Kill-mock = T1 ∧ T4 ∧ T5 ∧ T6: real Schedule, real Intake, real Task, real lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import ScheduleCadence, ScheduleTickResult, TaskOrigin
from qor.kernel.clock import FrozenClock
from tests.generic_staff import staffed_config

pytestmark = pytest.mark.qor_scheduler

START = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
OBJECTIVE = "Prepare the periodic organizational status for the current cycle."
HOUR = timedelta(hours=1)


def _app():
    return build_app(planner=StubPlanner(), clock=FrozenClock(START), config=staffed_config())


def _once(app, when: datetime):
    return app.orchestrator.scheduling.register(
        objective=OBJECTIVE,
        cadence=ScheduleCadence.ONCE,
        next_run_at=when,
        run_at=when,
    )


def _every(app, when: datetime):
    return app.orchestrator.scheduling.register(
        objective=OBJECTIVE,
        cadence=ScheduleCadence.EVERY,
        next_run_at=when,
        interval=HOUR,
    )


def test_t1_once_schedule_opens_one_task():
    app = _app()
    schedule = _once(app, START)
    result = app.orchestrator.scheduling.tick(schedule.id, START)
    assert result.result is ScheduleTickResult.CREATED
    tasks = app.runtime.repos.tasks.all()
    assert len(tasks) == 1
    assert tasks[0].origin is TaskOrigin.SCHEDULE
    assert tasks[0].ref == result.task_ref
    stored = app.runtime.repos.schedules.get(schedule.id)
    assert stored.status == "DISABLED"
    assert stored.enabled is False


def test_t2_every_schedule_advances_and_opens_the_next_task():
    app = _app()
    schedule = _every(app, START)
    first = app.orchestrator.scheduling.tick(schedule.id, START)
    assert first.result is ScheduleTickResult.CREATED
    opened = app.runtime.repos.tasks.get(app.runtime.repos.schedules.get(schedule.id).last_task_id)
    app.orchestrator.run_task(opened)
    second_at = START + HOUR
    second = app.orchestrator.scheduling.tick(schedule.id, second_at)
    assert second.result is ScheduleTickResult.CREATED
    assert second.task_ref != first.task_ref
    tasks = [task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.SCHEDULE]
    assert len(tasks) == 2
    stored = app.runtime.repos.schedules.get(schedule.id)
    assert stored.status == "ENABLED"
    assert stored.next_run_at == START + (HOUR * 2)


def test_t3_disabled_schedule_does_not_open_a_task():
    app = _app()
    schedule = _every(app, START)
    app.orchestrator.scheduling.disable(schedule.id)
    result = app.orchestrator.scheduling.tick(schedule.id, START)
    assert result.result is ScheduleTickResult.SKIPPED_DISABLED
    assert app.runtime.repos.tasks.all() == ()


def test_t4_same_occurrence_creates_one_task():
    app = _app()
    schedule = _once(app, START)
    first = app.orchestrator.scheduling.tick(schedule.id, START)
    second = app.orchestrator.scheduling.tick(schedule.id, START)
    assert first.result is ScheduleTickResult.CREATED
    assert second.result is ScheduleTickResult.SKIPPED_DUPLICATE
    assert first.occurrence_key == second.occurrence_key
    assert len(app.runtime.repos.tasks.all()) == 1


def test_t5_active_task_suppresses_the_next_occurrence():
    app = _app()
    schedule = _every(app, START)
    first = app.orchestrator.scheduling.tick(schedule.id, START)
    assert first.result is ScheduleTickResult.CREATED
    stored = app.runtime.repos.schedules.get(schedule.id)
    assert stored.next_run_at == START + HOUR
    second = app.orchestrator.scheduling.tick(schedule.id, START + HOUR)
    assert second.result is ScheduleTickResult.SKIPPED_ACTIVE
    assert len(app.runtime.repos.tasks.all()) == 1
    assert app.runtime.repos.schedules.get(schedule.id).next_run_at == START + HOUR


def test_t6_opened_task_enters_the_existing_lifecycle():
    app = _app()
    schedule = _once(app, START)
    opened = app.orchestrator.scheduling.tick(schedule.id, START)
    task = next(task for task in app.runtime.repos.tasks.all() if task.ref == opened.task_ref)
    assert task.origin is TaskOrigin.SCHEDULE
    assert task.status == "OPEN"
    admitted = [
        record
        for record in app.runtime.log.transitions_for(task.id)
        if record.transition == "admit" and record.to_state == "OPEN"
    ]
    assert admitted
    result = app.orchestrator.run_task(task)
    assert result.plan is not None
    assert result.task.id == task.id
    assert result.task.status != "OPEN"
    assert result.task.origin is TaskOrigin.SCHEDULE


def test_t7_trigger_is_auditable_from_schedule_to_task():
    app = _app()
    schedule = _once(app, START)
    opened = app.orchestrator.scheduling.tick(schedule.id, START)
    matches = [
        event
        for event in app.runtime.log.events()
        if event.name == "qor.schedule.triggered"
        and event.payload.get("schedule_ref") == schedule.ref
        and event.payload.get("occurrence_key") == opened.occurrence_key
        and event.payload.get("task_ref") == opened.task_ref
        and event.payload.get("result") == ScheduleTickResult.CREATED.value
    ]
    assert matches
    assert matches[0].routed is False
