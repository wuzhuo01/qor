"""Notification Auto-Trigger v0.1. T1–T30.

A new ACTIVE Responsibility inserted by ResponsibilityService is what calls
NotificationService. Tests do not call create_action_required to stand in for that.
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

pytestmark = pytest.mark.qor_notification_auto_trigger

OBJECTIVE = "Record the organizational commitment that still needs a person."


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
        reason="human registered for auto-trigger tests",
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


def _create(app, source_type, source_id, human_id):
    return app.orchestrator.responsibilities.create(
        caller_agent_id=_human(app).id,
        source_type=source_type,
        source_id=source_id,
        responsible_human_agent_id=human_id,
    )


def _audits(app):
    return [
        record
        for record in app.runtime.log.audit_records()
        if record.basis.startswith("NOTIFICATION_CREATED ")
    ]


def test_t1_auto_notification_does_not_change_source_state(clock):
    app, result = _wait(clock)
    before = app.runtime.repos.tasks.get(result.task.id).status
    _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert app.runtime.repos.tasks.get(result.task.id).status == before == "AWAITING_HUMAN"


def test_t2_auto_notification_does_not_change_responsibility_state(clock):
    app, result = _wait(clock)
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"


def test_t3_target_comes_from_the_new_responsibility(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert intent.target_human_agent_id == human.id == bound.responsible_human_agent_id


def test_t4_role_does_not_choose_the_target(clock):
    app, result = _wait(clock)
    chosen = _other_human(app, "Human A")
    other = _other_human(app, "Human C")
    assert chosen.role_ids == other.role_ids
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, chosen.id)
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert intent.target_human_agent_id == chosen.id
    assert intent.target_human_agent_id != other.id


def test_t5_permission_does_not_choose_the_target(clock):
    app, result = _wait(clock)
    chosen = _other_human(app, "Human B")
    cio = _human(app)
    roles = app.runtime.role_map()
    assert "approval.grant" in cio.effective_permissions(roles)
    assert "approval.grant" in chosen.effective_permissions(roles)
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, chosen.id)
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert intent.target_human_agent_id == chosen.id
    assert intent.target_human_agent_id != cio.id


def test_t6_human_cio_is_not_the_default_target(clock):
    app, result = _wait(clock)
    cio = _human(app)
    other = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, other.id)
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert app.runtime.organization().human_cio_agent_id == cio.id
    assert intent.target_human_agent_id == other.id
    assert intent.target_human_agent_id != cio.id


def test_t7_repeat_create_does_not_notify_again(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    first = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    second = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    assert second.id == first.id
    assert len(app.runtime.notifications.all()) == 1
    assert len(app.notification_delivery.attempts) == 1


def test_t8_delivery_failure_does_not_change_the_source(clock):
    app, result = _wait(clock)
    before = app.runtime.repos.tasks.get(result.task.id).status
    app.notification_delivery.fail_next()
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert intent.state is NotificationDeliveryState.FAILED
    assert app.runtime.repos.tasks.get(result.task.id).status == before


def test_t9_delivery_failure_leaves_the_responsibility_active(clock):
    app, result = _wait(clock)
    app.notification_delivery.fail_next()
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"
    assert (
        app.runtime.notifications.find_by_responsibility(bound.id)[0].state
        is NotificationDeliveryState.FAILED
    )


def test_t10_scheduler_does_not_create_a_notification(clock):
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


def test_t11_event_router_does_not_create_a_notification(clock):
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


def test_t12_an_audit_record_alone_does_not_notify(clock):
    app, result = _wait(clock)
    assert app.runtime.log.audit_records()
    assert result.task.status == "AWAITING_HUMAN"
    assert app.runtime.repos.responsibilities.all() == ()
    assert app.runtime.notifications.all() == ()


def test_t13_a_state_transition_alone_does_not_notify(clock):
    app, result = _wait(clock)
    assert result.task.status == "AWAITING_HUMAN"
    assert app.runtime.log.transitions_for(result.task.id)
    assert app.runtime.notifications.all() == ()
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
    assert escalated.status == "ESCALATED"
    assert fresh.runtime.notifications.all() == ()


def test_t14_an_event_alone_does_not_notify(clock):
    app, result = _wait(clock)
    assert app.runtime.log.events()
    assert result.task.status == "AWAITING_HUMAN"
    assert app.runtime.notifications.all() == ()


def test_t15_state_transitions_do_not_call_notification(clock):
    app = _app(clock)
    calls: list[object] = []
    original = app.notifications.create_action_required

    def wrapped(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    app.notifications.create_action_required = wrapped
    result = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    assert result.task.status == "AWAITING_HUMAN"
    app.orchestrator.escalate_task(result.task.id, escalated_by_agent_id=_human(app).id)
    assert calls == []
    assert app.runtime.notifications.all() == ()


def test_t16_the_trigger_does_not_name_a_recipient(clock):
    app, result = _wait(clock)
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original = app.notifications.create_action_required

    def wrapped(*args, **kwargs):
        seen.append((args, kwargs))
        return original(*args, **kwargs)

    app.notifications.create_action_required = wrapped
    human = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert kwargs == {}
    assert args[0] is ResponsibilitySource.TASK
    assert args[1] == result.task.id
    assert human.id not in args
    assert _human(app).id not in args
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert intent.target_human_agent_id == human.id


def test_t17_transfer_notifies_the_new_responsibility_only(clock):
    app, result = _wait(clock)
    first = _other_human(app, "Human A")
    second = _other_human(app, "Human B")
    original = _create(app, ResponsibilitySource.TASK, result.task.id, first.id)
    first_intent = app.runtime.notifications.find_by_responsibility(original.id)[0]
    moved = app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=second.id,
        reason="the first human is unavailable",
    )
    second_intent = app.runtime.notifications.find_by_responsibility(moved.id)[0]
    assert app.runtime.repos.responsibilities.get(original.id).status == "TRANSFERRED"
    assert moved.status == "ACTIVE"
    assert first_intent.target_human_agent_id == first.id
    assert app.runtime.notifications.get(first_intent.id).target_human_agent_id == first.id
    assert second_intent.target_human_agent_id == second.id
    assert second_intent.responsibility_id == moved.id
    assert second_intent.reason == "the first human is unavailable"


def test_t18_clear_does_not_create_another_intent(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    existing = app.runtime.notifications.find_by_responsibility(bound.id)
    app.orchestrator.responsibilities.clear_responsibility(
        bound.id, caller_agent_id=_human(app).id, reason="the wait was withdrawn"
    )
    assert app.runtime.notifications.find_by_responsibility(bound.id) == existing
    assert len(app.runtime.notifications.all()) == 1
    assert existing[0].target_human_agent_id == human.id


def test_t19_complete_does_not_create_another_intent(clock):
    app, result = _wait(clock)
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    before = app.runtime.notifications.all()
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    assert app.runtime.repos.responsibilities.get(bound.id).status == "COMPLETED"
    assert app.runtime.notifications.all() == before


def test_t20_auto_trigger_does_not_import_a_channel():
    banned = ("email", "dingtalk", "wechat", "slack", "sms", "requests", "httpx", "webhook")
    for path in (
        Path("qor/services/responsibility.py"),
        Path("qor/services/notification.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(any(word in name.lower() for word in banned) for name in imported)


def test_t21_auto_trigger_does_not_use_a_message_bus():
    text = Path("qor/services/responsibility.py").read_text(encoding="utf-8").lower()
    for word in ("kafka", "rabbitmq", "messagebus", "message_bus", "queue", "worker"):
        assert word not in text


def _watch(obj, name: str, calls: dict[str, int]) -> None:
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        calls[name] = calls.get(name, 0) + 1
        return original(*args, **kwargs)

    setattr(obj, name, wrapped)


@pytest.mark.parametrize(
    ("number", "owner_name", "method"),
    [
        (22, "planner", "plan"),
        (23, "agents", "execute_work"),
        (24, "reviewing", "review"),
        (25, "deciding", "decide"),
        (26, "gateway", "request"),
    ],
)
def test_t22_to_t26_auto_trigger_does_not_call_downstream(clock, number, owner_name, method):
    app, result = _wait(clock)
    calls: dict[str, int] = {}
    owner = {
        "planner": app.planner,
        "agents": app.agents,
        "reviewing": app.orchestrator.reviewing,
        "deciding": app.orchestrator.deciding,
        "gateway": app.orchestrator.gateway,
    }[owner_name]
    _watch(owner, method, calls)
    _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert calls.get(method, 0) == 0, f"T{number} observed {method}"


def test_t27_in_memory_delivery_receives_the_auto_intent(clock):
    app, result = _wait(clock)
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert len(app.notification_delivery.attempts) == 1
    intent = app.runtime.notifications.find_by_responsibility(bound.id)[0]
    assert app.notification_delivery.attempts[0].id == intent.id
    assert intent.state is NotificationDeliveryState.DELIVERED
    assert intent.actionability is NotificationActionability.ACTION_REQUIRED


def test_t28_repeating_the_trigger_does_not_deliver_twice(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    again = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    assert again.id == bound.id
    app.orchestrator.responsibilities._notify_new_active(bound, bound.ref)
    assert len(app.runtime.notifications.all()) == 1
    assert len(app.notification_delivery.attempts) == 1


def test_t29_audit_names_the_source(clock):
    app, result = _wait(clock)
    _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert f"source=TASK:{result.task.id}" in _audits(app)[-1].basis


def test_t30_audit_names_the_responsibility(clock):
    app, result = _wait(clock)
    human = _other_human(app, "Human B")
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, human.id)
    basis = _audits(app)[-1].basis
    assert f"responsibility={bound.id}" in basis
    assert f"human={human.id}" in basis


def test_a_different_human_is_rejected_without_another_intent(clock):
    app, result = _wait(clock)
    first = _other_human(app, "Human A")
    second = _other_human(app, "Human B")
    _create(app, ResponsibilitySource.TASK, result.task.id, first.id)
    with pytest.raises(QORError, match="ACTIVE"):
        _create(app, ResponsibilitySource.TASK, result.task.id, second.id)
    assert len(app.runtime.notifications.all()) == 1
    assert len(app.notification_delivery.attempts) == 1


def test_transfer_delivery_failure_does_not_roll_back(clock):
    app, result = _wait(clock)
    first = _other_human(app, "Human A")
    second = _other_human(app, "Human B")
    original = _create(app, ResponsibilitySource.TASK, result.task.id, first.id)
    old_intent = app.runtime.notifications.find_by_responsibility(original.id)[0]
    app.notification_delivery.fail_next()
    moved = app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=second.id,
        reason="the first human is unavailable",
    )
    assert app.runtime.repos.responsibilities.get(original.id).status == "TRANSFERRED"
    assert app.runtime.repos.responsibilities.get(moved.id).status == "ACTIVE"
    new_intent = app.runtime.notifications.find_by_responsibility(moved.id)[0]
    assert new_intent.state is NotificationDeliveryState.FAILED
    assert new_intent.target_human_agent_id == second.id
    assert app.runtime.notifications.get(old_intent.id).target_human_agent_id == first.id
    assert app.runtime.repos.tasks.get(result.task.id).status == "AWAITING_HUMAN"


def test_notification_refusal_does_not_roll_back_the_responsibility(clock):
    app, result = _wait(clock)

    def refuse(*_args, **_kwargs):
        raise QORError("source cannot take an ACTION_REQUIRED notification")

    app.notifications.create_action_required = refuse
    bound = _create(app, ResponsibilitySource.TASK, result.task.id, _other_human(app, "Human B").id)
    assert app.runtime.repos.responsibilities.get(bound.id).status == "ACTIVE"
    assert app.runtime.notifications.all() == ()
