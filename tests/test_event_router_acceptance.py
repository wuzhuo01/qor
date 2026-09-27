"""QOR Event Router v0.1. T1–T13.

Kill-mock = T1 ∧ T4 ∧ T5 ∧ T6 ∧ T8 ∧ T10 ∧ T11 ∧ T13:
real Event, EventRoute, EventRouter, Intake, and Task.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.audit import Event
from qor.domain.enums import (
    EventRouteResult,
    EventRouteStatus,
    ScheduleCadence,
    ScheduleTickResult,
    TaskOrigin,
)
from qor.domain.event_route import RESOLVED_EVENT_NAME
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.clock import FrozenClock
from qor.kernel.errors import InvariantViolation, QORError, TransitionRejected
from qor.services.planning import PlanRejected
from tests.generic_staff import staffed_config

pytestmark = pytest.mark.qor_event_router

EVENT_NAME = "qor.organization.cycle_due"
OBJECTIVE = "Prepare the organizational status requested by this notice."
OTHER_OBJECTIVE = "Record the organizational follow-up requested by this notice."


def _app(planner=None):
    return build_app(planner=planner or StubPlanner(), config=staffed_config())


def _notice(app, name: str = EVENT_NAME) -> Event:
    return app.engine.emit(
        name,
        entity=app.runtime.organization(),
        actor=ActorRef.system("QOR-TEST"),
        payload={"notice": name},
    )


def _resolved(app):
    return [event for event in app.runtime.log.events() if event.name == RESOLVED_EVENT_NAME]


def _event_tasks(app):
    return [task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.EVENT]


def test_t1_one_enabled_route_opens_one_task():
    app = _app()
    route = app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    results = app.orchestrator.routing.route(event)
    assert len(results) == 1
    assert results[0].result is EventRouteResult.SUCCESS
    assert results[0].route_id == route.id
    tasks = _event_tasks(app)
    assert len(tasks) == 1
    assert tasks[0].origin is TaskOrigin.EVENT
    assert tasks[0].status == "OPEN"
    assert tasks[0].objective == OBJECTIVE
    assert tasks[0].ref == results[0].task_ref


def test_t2_unmatched_event_opens_nothing():
    app = _app()
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    before = len(app.runtime.repos.tasks.all())
    results = app.orchestrator.routing.route(_notice(app, "qor.organization.unrelated"))
    assert results == ()
    assert len(app.runtime.repos.tasks.all()) == before
    assert _resolved(app) == []


def test_t3_disabled_route_is_skipped_and_can_run_after_enable():
    app = _app()
    route = app.orchestrator.routing.register(
        event_name=EVENT_NAME,
        objective=OBJECTIVE,
        status=EventRouteStatus.DISABLED,
    )
    event = _notice(app)
    before = len(app.runtime.repos.tasks.all())
    skipped = app.orchestrator.routing.route(event)
    assert len(app.runtime.repos.tasks.all()) == before
    assert skipped[0].result is EventRouteResult.SKIPPED_DISABLED
    receipts = _resolved(app)
    assert len(receipts) == 1
    assert receipts[0].payload["result"] == EventRouteResult.SKIPPED_DISABLED.value
    assert receipts[0].payload["result"] != EventRouteResult.SUCCESS.value
    app.orchestrator.routing.set_status(route.id, EventRouteStatus.ENABLED)
    opened = app.orchestrator.routing.route(event)
    assert opened[0].result is EventRouteResult.SUCCESS
    assert len(_event_tasks(app)) == 1


def test_t4_same_event_and_route_opens_one_task():
    app = _app()
    route = app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    first = app.orchestrator.routing.route(event)
    second = app.orchestrator.routing.route(event)
    assert first[0].result is EventRouteResult.SUCCESS
    assert second[0].result is EventRouteResult.SKIPPED_DUPLICATE
    assert second[0].route_id == route.id
    assert len(_event_tasks(app)) == 1
    successes = [
        item
        for item in _resolved(app)
        if item.payload["result"] == EventRouteResult.SUCCESS.value
        and item.payload["route_id"] == str(route.id)
    ]
    assert len(successes) == 1


def test_t5_one_event_two_routes_opens_two_tasks():
    app = _app()
    first_route = app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    second_route = app.orchestrator.routing.register(
        event_name=EVENT_NAME, objective=OTHER_OBJECTIVE
    )
    results = app.orchestrator.routing.route(_notice(app))
    assert [item.result for item in results] == [
        EventRouteResult.SUCCESS,
        EventRouteResult.SUCCESS,
    ]
    assert {item.route_id for item in results} == {first_route.id, second_route.id}
    tasks = _event_tasks(app)
    assert len(tasks) == 2
    assert {task.objective for task in tasks} == {OBJECTIVE, OTHER_OBJECTIVE}
    assert {task.status for task in tasks} == {"OPEN"}


def test_t6_opened_task_enters_the_existing_lifecycle():
    app = _app()
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    opened = app.orchestrator.routing.route(_notice(app))
    task = next(task for task in app.runtime.repos.tasks.all() if task.ref == opened[0].task_ref)
    assert task.origin is TaskOrigin.EVENT
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
    assert result.task.origin is TaskOrigin.EVENT


def test_t7_admission_failure_is_not_success_and_can_be_retried():
    app = _app()
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    real = app.orchestrator.intake.open_from_event

    def fail_once(route, incoming):
        app.orchestrator.intake.open_from_event = real
        raise TransitionRejected("Task", "admit", "simulated admission failure")

    app.orchestrator.intake.open_from_event = fail_once
    with pytest.raises(TransitionRejected):
        app.orchestrator.routing.route(event)
    assert _resolved(app) == []
    assert _event_tasks(app) == []
    opened = app.orchestrator.routing.route(event)
    assert opened[0].result is EventRouteResult.SUCCESS
    assert len(_event_tasks(app)) == 1
    assert _event_tasks(app)[0].status == "OPEN"


def test_t8_later_run_failure_does_not_open_a_second_task():
    class RejectingPlanner:
        kind = "rejecting"

        def plan(self, request):
            raise PlanRejected("planned failure after the route succeeded")

    app = _app(RejectingPlanner())
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    first = app.orchestrator.routing.route(event)
    assert first[0].result is EventRouteResult.SUCCESS
    task = _event_tasks(app)[0]
    finished = app.orchestrator.run_task(task)
    assert finished.task.status == "BLOCKED"
    second = app.orchestrator.routing.route(event)
    assert second[0].result is EventRouteResult.SKIPPED_DUPLICATE
    assert len(_event_tasks(app)) == 1


def test_t9_receipt_traces_event_route_and_task():
    app = _app()
    route = app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    opened = app.orchestrator.routing.route(event)
    receipt = next(
        item for item in _resolved(app) if item.payload["result"] == EventRouteResult.SUCCESS.value
    )
    assert receipt.payload["event_id"] == str(event.id)
    assert receipt.payload["route_id"] == str(route.id)
    assert receipt.payload["route_ref"] == route.ref
    assert receipt.payload["route_id"] != receipt.payload["route_ref"]
    assert receipt.payload["task_ref"] == opened[0].task_ref
    task = next(
        task for task in app.runtime.repos.tasks.all() if task.ref == receipt.payload["task_ref"]
    )
    assert task.origin is TaskOrigin.EVENT
    assert task.status == "OPEN"


def test_t10_router_does_not_call_downstream_services():
    app = _app()
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    calls: dict[str, int] = {}

    def watch(obj, name: str) -> None:
        original = getattr(obj, name)

        def wrapped(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return original(*args, **kwargs)

        setattr(obj, name, wrapped)

    watch(app.planner, "plan")
    watch(app.agents, "execute_work")
    watch(app.agents, "execute_review")
    watch(app.agents, "propose_decision")
    watch(app.orchestrator.reviewing, "review")
    watch(app.orchestrator.deciding, "decide")
    watch(app.orchestrator.gateway, "request")
    watch(app.orchestrator, "run_task")
    app.orchestrator.routing.route(_notice(app))
    assert calls == {}
    assert len(_event_tasks(app)) == 1


def test_t11_only_the_router_door_opens_event_tasks():
    app = _app()
    agent = next(
        item for item in app.runtime.repos.agents.all() if item.kind is not ActorKind.HUMAN
    )
    before = len(app.runtime.repos.tasks.all())
    with pytest.raises(QORError, match="not TaskOrigin.EVENT"):
        app.orchestrator.intake.submit(
            objective="Record the weekly organizational status.",
            requested_by_agent_id=agent.id,
        )
    assert len(app.runtime.repos.tasks.all()) == before
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    app.orchestrator.routing.route(_notice(app))
    tasks = _event_tasks(app)
    assert len(tasks) == 1
    assert tasks[0].origin is TaskOrigin.EVENT
    assert tasks[0].created_by.display == "QOR-EVENT-ROUTER"


def test_t12_schedule_door_still_opens_schedule_tasks():
    """Time → Task stays on the scheduler. Routing a notice does not open it."""
    start = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    app = build_app(planner=StubPlanner(), clock=FrozenClock(start), config=staffed_config())
    schedule = app.orchestrator.scheduling.register(
        objective="Prepare the periodic organizational status for the current cycle.",
        cadence=ScheduleCadence.ONCE,
        next_run_at=start,
        run_at=start,
    )
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    app.orchestrator.routing.route(_notice(app))
    tick = app.orchestrator.scheduling.tick(schedule.id, start)
    assert tick.result is ScheduleTickResult.CREATED
    origins = {task.origin for task in app.runtime.repos.tasks.all()}
    assert origins == {TaskOrigin.EVENT, TaskOrigin.SCHEDULE}


def test_t13_resolved_event_is_not_routed():
    app = _app()
    with pytest.raises(InvariantViolation, match="qor.event_route.resolved"):
        app.orchestrator.routing.register(event_name=RESOLVED_EVENT_NAME, objective=OBJECTIVE)
    app.orchestrator.routing.register(event_name=EVENT_NAME, objective=OBJECTIVE)
    event = _notice(app)
    app.orchestrator.routing.route(event)
    receipts = _resolved(app)
    assert len(receipts) == 1
    before_tasks = len(app.runtime.repos.tasks.all())
    again = app.orchestrator.routing.route(receipts[0])
    assert again == ()
    assert len(app.runtime.repos.tasks.all()) == before_tasks
    assert len(_resolved(app)) == 1
