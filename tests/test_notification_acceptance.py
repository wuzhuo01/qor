"""Notification v0.1. T1–T30.

Explicit create_action_required only. Scheduler, Event Router, Orchestrator,
and Responsibility do not call it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import (
    NotificationActionability,
    NotificationDeliveryState,
    ResponsibilitySource,
    RiskLevel,
    ScheduleCadence,
    ScheduleTickResult,
    TaskOrigin,
)
from qor.domain.organization import Agent
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.clock import FrozenClock
from qor.kernel.errors import QORError
from tests.contract_agents import ContractAgentRuntime
from tests.generic_staff import staffed_config

pytestmark = pytest.mark.qor_notification

OBJECTIVE = "Record the organizational commitment that still needs a person."
REASON = "a person still has to handle this fact"


def _human(app):
    return app.runtime.repos.agents.get(app.runtime.organization().human_cio_agent_id)


def _app(clock: FrozenClock):
    return build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(clock),
        clock=clock,
        config=staffed_config(),
    )


def _wait(clock: FrozenClock):
    app = _app(clock)
    result = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    return app, result


def _other_human(app, name: str) -> Agent:
    org = app.runtime.organization()
    now = app.runtime.clock.now()
    aid, ref = app.runtime.mint("AGT")
    agent = Agent(
        id=aid,
        ref=ref,
        status="OFFLINE",
        created_at=now,
        created_by=ActorRef.system("QOR-TEST"),
        updated_at=now,
        organization_id=org.id,
        kind=ActorKind.HUMAN,
        name=name,
        role_ids=(app.runtime.role_index["role.human_cio"],),
    )
    app.runtime.repos.agents.insert(agent)
    app.engine.record_creation(
        agent,
        actor=ActorRef.system("QOR-TEST"),
        reason_code="AGENT_REGISTERED",
        reason="human registered for notification tests",
    )
    return app.runtime.repos.agents.save(
        app.engine.fire(
            agent,
            "register",
            actor=ActorRef.system("QOR-TEST"),
            reason_code="AGENT_REGISTERED",
            reason="human brought online",
        ).entity
    )


def _bind(app, source_type, source_id, human_id):
    return app.orchestrator.responsibilities.create(
        caller_agent_id=_human(app).id,
        source_type=source_type,
        source_id=source_id,
        responsible_human_agent_id=human_id,
    )


def _notify(app, source_type, source_id, reason: str = REASON):
    return app.notifications.create_action_required(source_type, source_id, reason)


def _created_audits(app):
    return [
        record
        for record in app.runtime.log.audit_records()
        if record.basis.startswith("NOTIFICATION_CREATED ")
    ]


def test_t1_notification_does_not_create_an_event(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    calls = {"route": 0}
    original = app.orchestrator.routing.route

    def _route(*args, **kwargs):
        calls["route"] += 1
        return original(*args, **kwargs)

    app.orchestrator.routing.route = _route
    before = len(app.runtime.log.events())
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert len(app.runtime.log.events()) == before
    assert calls["route"] == 0


def test_t2_notification_is_not_an_audit_record(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    audits = _created_audits(app)
    assert audits
    assert audits[-1].target_id == intent.id
    assert audits[-1].target_type == "NotificationIntent"
    assert app.runtime.notifications.get(intent.id).id == intent.id
    assert type(intent).__name__ != "AuditRecord"


def test_t3_notification_does_not_create_a_responsibility(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    before = len(app.runtime.repos.responsibilities.all())
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert len(app.runtime.repos.responsibilities.all()) == before


def test_t4_target_comes_from_the_active_responsibility(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert intent.target_human_agent_id == bound.responsible_human_agent_id == human.id
    assert intent.target_human_agent_id is not None


def test_t5_role_does_not_choose_the_human(clock):
    app, result = _wait(clock)
    chosen = _other_human(app, "Human A")
    other = _other_human(app, "Human C")
    assert chosen.role_ids == other.role_ids
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, chosen.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert intent.target_human_agent_id == bound.responsible_human_agent_id
    assert intent.target_human_agent_id != other.id


def test_t6_permission_does_not_choose_the_human(clock):
    app, result = _wait(clock)
    chosen = _other_human(app, "Human A")
    cio = _human(app)
    roles = app.runtime.role_map()
    assert "approval.grant" in cio.effective_permissions(roles)
    assert "approval.grant" in chosen.effective_permissions(roles)
    _bind(app, ResponsibilitySource.TASK, result.task.id, chosen.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert intent.target_human_agent_id == chosen.id
    assert intent.target_human_agent_id != cio.id


def test_t7_human_cio_is_not_the_default_target(clock):
    app, result = _wait(clock)
    cio = _human(app)
    other = _other_human(app, "Human B")
    _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert app.runtime.organization().human_cio_agent_id == cio.id
    assert intent.target_human_agent_id == other.id
    assert intent.target_human_agent_id != cio.id


def test_t8_active_responsibility_can_be_notified(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert bound.status == "ACTIVE"
    assert intent.responsibility_id == bound.id
    assert intent.target_human_agent_id == human.id
    assert intent.actionability is NotificationActionability.ACTION_REQUIRED
    assert intent.state is NotificationDeliveryState.DELIVERED
    assert len(app.notification_delivery.attempts) == 1


def test_t9_transfer_makes_the_new_responsibility_the_target(clock):
    app, result = _wait(clock)
    first = _other_human(app, "Human A")
    second = _other_human(app, "Human B")
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, first.id)
    moved = app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=second.id,
        reason="the first human is unavailable",
    )
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert moved.status == "ACTIVE"
    assert intent.responsibility_id == moved.id
    assert intent.target_human_agent_id == second.id


def test_t10_transfer_does_not_retarget_the_old_intent(clock):
    app, result = _wait(clock)
    first = _other_human(app, "Human A")
    second = _other_human(app, "Human B")
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, first.id)
    old_intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=second.id,
        reason="the first human is unavailable",
    )
    new_intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    historical = app.runtime.notifications.get(old_intent.id)
    assert historical.target_human_agent_id == first.id
    assert historical.responsibility_id == original.id
    assert app.runtime.notifications.find_by_responsibility(original.id) == (historical,)
    assert new_intent.id != historical.id
    assert new_intent.responsibility_id != original.id
    assert new_intent.target_human_agent_id == second.id


def test_t11_cleared_responsibility_cannot_open_a_new_intent(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    existing = _notify(app, ResponsibilitySource.TASK, result.task.id)
    app.orchestrator.responsibilities.clear_responsibility(
        bound.id, caller_agent_id=_human(app).id, reason="the wait was withdrawn"
    )
    with pytest.raises(QORError, match="ACTIVE"):
        _notify(app, ResponsibilitySource.TASK, result.task.id)
    kept = app.runtime.notifications.get(existing.id)
    assert kept.target_human_agent_id == human.id
    assert kept.actionability is NotificationActionability.ACTION_REQUIRED
    assert len(app.runtime.notifications.find_by_responsibility(bound.id)) == 1


def test_t12_completed_responsibility_cannot_open_a_new_intent(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    existing = _notify(app, ResponsibilitySource.TASK, result.task.id)
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    assert app.runtime.repos.responsibilities.get(bound.id).status == "COMPLETED"
    with pytest.raises(QORError, match="ACTIVE"):
        _notify(app, ResponsibilitySource.TASK, result.task.id)
    kept = app.runtime.notifications.get(existing.id)
    assert kept.target_human_agent_id == human.id
    assert len(app.runtime.notifications.all()) == 1


def test_t13_notification_does_not_change_source_state(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    before = app.runtime.repos.tasks.get(result.task.id).status
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert app.runtime.repos.tasks.get(result.task.id).status == before == "AWAITING_HUMAN"


def test_t14_delivery_failure_does_not_change_source_state(clock):
    app, result = _wait(clock)
    before_task = app.runtime.repos.tasks.get(result.task.id).status
    before_action = result.actions[0].status
    app.notification_delivery.fail_next()
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert intent.state is NotificationDeliveryState.FAILED
    assert app.runtime.repos.tasks.get(result.task.id).status == before_task
    assert app.runtime.repos.actions.get(result.actions[0].id).status == before_action


def test_t15_delivery_failure_does_not_change_responsibility(clock):
    app, result = _wait(clock)
    app.notification_delivery.fail_next()
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert len(app.notification_delivery.attempts) == 1
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"


def _watch(obj, name: str, calls: dict[str, int]) -> None:
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        calls[name] = calls.get(name, 0) + 1
        return original(*args, **kwargs)

    setattr(obj, name, wrapped)


@pytest.mark.parametrize(
    ("number", "owner_name", "method"),
    [
        (16, "planner", "plan"),
        (17, "agents", "execute_work"),
        (18, "reviewing", "review"),
        (19, "deciding", "decide"),
        (20, "gateway", "request"),
        (21, "scheduling", "tick"),
        (22, "routing", "route"),
    ],
)
def test_t16_to_t22_notification_does_not_call_downstream(clock, number, owner_name, method):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    calls: dict[str, int] = {}
    owner = {
        "planner": app.planner,
        "agents": app.agents,
        "reviewing": app.orchestrator.reviewing,
        "deciding": app.orchestrator.deciding,
        "gateway": app.orchestrator.gateway,
        "scheduling": app.orchestrator.scheduling,
        "routing": app.orchestrator.routing,
    }[owner_name]
    _watch(owner, method, calls)
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert calls.get(method, 0) == 0, f"T{number} observed {method}"


def test_t23_scheduler_does_not_create_a_notification(clock):
    start = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    app = build_app(planner=StubPlanner(), clock=FrozenClock(start), config=staffed_config())
    schedule = app.orchestrator.scheduling.register(
        objective="Prepare the periodic organizational status for the current cycle.",
        cadence=ScheduleCadence.ONCE,
        next_run_at=start,
        run_at=start,
    )
    tick = app.orchestrator.scheduling.tick(schedule.id, start)
    assert tick.result is ScheduleTickResult.CREATED
    assert app.runtime.notifications.all() == ()
    opened = next(
        task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.SCHEDULE
    )
    assert opened.origin is TaskOrigin.SCHEDULE


def test_t24_event_router_does_not_create_a_notification(clock):
    app = _app(clock)
    app.orchestrator.routing.register(
        event_name="qor.organization.cycle_due",
        objective="Prepare the organizational status requested by this notice.",
    )
    event = app.engine.emit(
        "qor.organization.cycle_due",
        entity=app.runtime.organization(),
        actor=ActorRef.system("QOR-TEST"),
        payload={"notice": "cycle"},
    )
    routed = app.orchestrator.routing.route(event)
    assert routed[0].result.value == "SUCCESS"
    assert app.runtime.notifications.all() == ()
    opened = next(task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.EVENT)
    assert opened.origin is TaskOrigin.EVENT


def test_t25_service_does_not_depend_on_a_channel():
    tree = ast.parse(Path("qor/services/notification.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    banned = ("email", "dingtalk", "wechat", "slack", "sms", "requests", "httpx", "webhook")
    assert not any(any(word in name.lower() for word in banned) for name in imported)
    text = Path("qor/services/notification.py").read_text(encoding="utf-8")
    assert "InMemoryNotificationDelivery" not in text
    assert "NotificationPort" in text


def test_t26_in_memory_delivery_records_the_intent(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert app.notification_delivery.attempts[0].id == intent.id
    assert app.notification_delivery.attempts[0].responsibility_id == bound.id
    assert intent.state is NotificationDeliveryState.DELIVERED


def test_t27_repeat_create_returns_the_same_intent(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    first = _notify(app, ResponsibilitySource.TASK, result.task.id)
    second = _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert second.id == first.id
    assert len(app.runtime.notifications.all()) == 1
    assert len(app.notification_delivery.attempts) == 1


def test_t28_audit_names_the_source(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    _notify(app, ResponsibilitySource.TASK, result.task.id)
    basis = _created_audits(app)[-1].basis
    assert f"source=TASK:{result.task.id}" in basis


def test_t29_audit_names_the_responsibility(clock):
    app, result = _wait(clock)
    bound = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    basis = _created_audits(app)[-1].basis
    assert f"responsibility={bound.id}" in basis
    assert f"human={intent.target_human_agent_id}" in basis


def test_t30_fact_and_action_required_stay_distinct(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    intent = _notify(app, ResponsibilitySource.TASK, result.task.id)
    task = app.runtime.repos.tasks.get(result.task.id)
    assert task.status == "AWAITING_HUMAN"
    assert intent.source_status == "AWAITING_HUMAN"
    assert intent.actionability is NotificationActionability.ACTION_REQUIRED
    assert intent.state is not task.status


def test_missing_responsibility_is_rejected(clock):
    app, result = _wait(clock)
    with pytest.raises(QORError, match="ACTIVE"):
        _notify(app, ResponsibilitySource.TASK, result.task.id)
    assert app.runtime.notifications.all() == ()


def test_action_and_task_node_use_the_same_human(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    action = result.actions[0]
    bound_action = _bind(app, ResponsibilitySource.ACTION, action.id, human.id)
    notified = _notify(app, ResponsibilitySource.ACTION, action.id)
    assert notified.target_human_agent_id == bound_action.responsible_human_agent_id
    assert notified.source_status == "APPROVAL_PENDING"
    assert app.runtime.repos.actions.get(action.id).status == "APPROVAL_PENDING"

    fresh = _app(clock)
    task = fresh.orchestrator.intake.submit(
        objective=OBJECTIVE, requested_by_agent_id=_human(fresh).id
    )
    _task, plan = fresh.orchestrator.planning.plan(task)
    node = fresh.runtime.repos.nodes.get(plan.node_ids[0])
    owner = fresh.runtime.repos.agents.get(task.owner_agent_id)
    escalated = fresh.runtime.repos.nodes.save(
        fresh.engine.fire(
            node,
            "escalate",
            actor=owner.as_actor(),
            reason_code="HUMAN_WAIT_ESCALATION",
            reason="node needs a person",
            correlation_id=task.id,
        ).entity
    )
    node_human = _other_human(fresh, "Human B")
    bound_node = _bind(fresh, ResponsibilitySource.TASK_NODE, escalated.id, node_human.id)
    node_intent = _notify(fresh, ResponsibilitySource.TASK_NODE, escalated.id)
    assert node_intent.target_human_agent_id == bound_node.responsible_human_agent_id
    assert node_intent.source_status == "ESCALATED"
    assert fresh.runtime.repos.nodes.get(escalated.id).status == "ESCALATED"


def test_source_that_left_human_wait_is_rejected(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    action = result.actions[0]
    bound = _bind(app, ResponsibilitySource.ACTION, action.id, human.id)
    granted = app.orchestrator.gateway.grant(result.task, action, _human(app).as_actor())
    assert granted.status == "APPROVED"
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"
    created = app.runtime.notifications.find_by_responsibility(bound.id)
    assert len(created) == 1
    assert created[0].source_status == "APPROVAL_PENDING"
    with pytest.raises(QORError, match="ACTION_REQUIRED"):
        _notify(app, ResponsibilitySource.ACTION, action.id)
    assert app.runtime.notifications.find_by_responsibility(bound.id) == created
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"
