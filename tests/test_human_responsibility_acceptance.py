"""Human Responsibility Binding v0.3. T1–T41.

Kill-mock uses real Responsibility, repository, service, Organization, Agent,
and the bootstrapped Human CIO. Downstream calls are counted, not commented.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import (
    ResponsibilitySource,
    RiskLevel,
    ScheduleCadence,
    ScheduleTickResult,
    TaskOrigin,
)
from qor.domain.organization import Agent
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.clock import FrozenClock
from qor.kernel.errors import IllegalStateWrite, QORError
from tests.contract_agents import ContractAgentRuntime
from tests.generic_staff import staffed_config

pytestmark = pytest.mark.qor_human_responsibility

OBJECTIVE = "Record the organizational commitment that still needs a person."


def _human(app):
    return app.runtime.repos.agents.get(app.runtime.organization().human_cio_agent_id)


def _owner(app):
    key = app.runtime.config.organization["default_task_owner"]
    return app.runtime.repos.agents.get(app.runtime.agent_index[key])


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


def _other_human(app) -> Agent:
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
        name="Second Human",
        role_ids=(app.runtime.role_index["role.human_cio"],),
    )
    app.runtime.repos.agents.insert(agent)
    app.engine.record_creation(
        agent,
        actor=ActorRef.system("QOR-TEST"),
        reason_code="AGENT_REGISTERED",
        reason="second human registered for responsibility tests",
    )
    saved = app.runtime.repos.agents.save(
        app.engine.fire(
            agent,
            "register",
            actor=ActorRef.system("QOR-TEST"),
            reason_code="AGENT_REGISTERED",
            reason="second human brought online",
        ).entity
    )
    return saved


def _bind(app, source_type, source_id, human_id):
    return app.orchestrator.responsibilities.create(
        caller_agent_id=_human(app).id,
        source_type=source_type,
        source_id=source_id,
        responsible_human_agent_id=human_id,
    )


def _audits(app, operation: str):
    return [
        record
        for record in app.runtime.log.audit_records()
        if record.basis.startswith(f"{operation} ")
    ]


def test_t1_human_agent_can_be_the_target(clock):
    app, result = _wait(clock)
    other = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    assert created.status == "ACTIVE"
    assert created.responsible_human_agent_id == other.id
    assert created.created_by_agent_id == _human(app).id


def test_t2_ai_agent_cannot_be_the_target(clock):
    app, result = _wait(clock)
    with pytest.raises(QORError, match="HUMAN"):
        _bind(app, ResponsibilitySource.TASK, result.task.id, _owner(app).id)


def test_t3_missing_agent_cannot_be_the_target(clock):
    app, result = _wait(clock)
    with pytest.raises(QORError, match="does not exist"):
        _bind(app, ResponsibilitySource.TASK, result.task.id, uuid4())


def test_t4_non_cio_cannot_create(clock):
    app, result = _wait(clock)
    other = _other_human(app)
    with pytest.raises(QORError, match="human CIO"):
        app.orchestrator.responsibilities.create(
            caller_agent_id=other.id,
            source_type=ResponsibilitySource.TASK,
            source_id=result.task.id,
            responsible_human_agent_id=other.id,
        )
    with pytest.raises(QORError, match="human CIO"):
        app.orchestrator.responsibilities.create(
            caller_agent_id=_owner(app).id,
            source_type=ResponsibilitySource.TASK,
            source_id=result.task.id,
            responsible_human_agent_id=other.id,
        )


def test_t5_human_cio_can_create(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    assert created.status == "ACTIVE"
    assert app.runtime.repos.tasks.get(result.task.id).status == "AWAITING_HUMAN"


def test_t6_human_cio_is_not_assigned_automatically(clock):
    app, result = _wait(clock)
    assert (
        app.runtime.repos.responsibilities.list_by_source(ResponsibilitySource.TASK, result.task.id)
        == ()
    )
    other = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    assert created.responsible_human_agent_id != _human(app).id


def test_t7_awaiting_human_task_can_be_bound(clock):
    app, result = _wait(clock)
    assert result.task.status == "AWAITING_HUMAN"
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    assert created.source_type is ResponsibilitySource.TASK
    assert created.status == "ACTIVE"


def test_t8_approval_pending_action_can_be_bound(clock):
    app, result = _wait(clock)
    action = result.actions[0]
    assert action.status == "APPROVAL_PENDING"
    created = _bind(app, ResponsibilitySource.ACTION, action.id, _other_human(app).id)
    assert created.source_type is ResponsibilitySource.ACTION
    assert app.runtime.repos.actions.get(action.id).status == "APPROVAL_PENDING"


def test_t9_escalated_task_can_be_bound(clock):
    app, result = _wait(clock)
    escalated = app.orchestrator.escalate_task(result.task.id, escalated_by_agent_id=_human(app).id)
    assert escalated.task.status == "ESCALATED"
    created = _bind(app, ResponsibilitySource.TASK, escalated.task.id, _other_human(app).id)
    assert created.status == "ACTIVE"


def test_t10_escalated_task_node_can_be_bound(clock):
    app = _app(clock)
    task = app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        requested_by_agent_id=_human(app).id,
    )
    _task, plan = app.orchestrator.planning.plan(task)
    node = app.runtime.repos.nodes.get(plan.node_ids[0])
    assert node.status == "PENDING"
    owner = app.runtime.repos.agents.get(task.owner_agent_id)
    escalated = app.runtime.repos.nodes.save(
        app.engine.fire(
            node,
            "escalate",
            actor=owner.as_actor(),
            reason_code="HUMAN_WAIT_ESCALATION",
            reason="node needs a person",
            correlation_id=task.id,
        ).entity
    )
    assert escalated.status == "ESCALATED"
    created = _bind(app, ResponsibilitySource.TASK_NODE, escalated.id, _other_human(app).id)
    assert created.source_type is ResponsibilitySource.TASK_NODE
    assert app.runtime.repos.nodes.get(escalated.id).status == "ESCALATED"


def test_t11_other_source_states_are_rejected(clock, tmp_path: Path):
    app = _app(clock)
    open_task = app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        requested_by_agent_id=_human(app).id,
    )
    other = _other_human(app)
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(app, ResponsibilitySource.TASK, open_task.id, other.id)
    running_task, plan = app.orchestrator.planning.plan(open_task)
    owner = app.runtime.repos.agents.get(running_task.owner_agent_id)
    running_task = app.runtime.repos.tasks.save(
        app.engine.fire(
            running_task,
            "start",
            actor=owner.as_actor(),
            reason_code="ROUTINE_PROGRESSION",
            reason="execution started before any human wait",
            correlation_id=running_task.id,
        ).entity
    )
    assert running_task.status == "RUNNING"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(app, ResponsibilitySource.TASK, running_task.id, other.id)
    pending = app.runtime.repos.nodes.get(plan.node_ids[0])
    assigned, _assignment = app.orchestrator.delegation.assign(running_task, pending)
    running_node = app.runtime.repos.nodes.save(
        app.engine.fire(
            assigned,
            "start",
            actor=owner.as_actor(),
            reason_code="ROUTINE_PROGRESSION",
            reason="node execution started",
            correlation_id=running_task.id,
        ).entity
    )
    assert running_node.status == "RUNNING"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(app, ResponsibilitySource.TASK_NODE, running_node.id, other.id)
    finished = app.orchestrator.run_objective(OBJECTIVE)
    assert finished.task.status == "COMPLETED"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(app, ResponsibilitySource.TASK, finished.task.id, other.id)
    node = next(item for item in app.runtime.repos.nodes.all() if item.task_id == finished.task.id)
    assert node.status == "ACCEPTED"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(app, ResponsibilitySource.TASK_NODE, node.id, other.id)

    from datetime import UTC, datetime

    start = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    world = tmp_path
    world.mkdir(parents=True, exist_ok=True)
    (world / "ledger.json").write_text(json.dumps({"value": 0, "applied": []}), encoding="utf-8")
    waiting_app = build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(FrozenClock(start), world_effect=True),
        clock=FrozenClock(start),
        world_root=world,
        config=staffed_config(),
    )
    waiting = waiting_app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    action = waiting.actions[0]
    target = _other_human(waiting_app).id
    cio = _human(waiting_app)
    owner = _owner(waiting_app)
    granted = waiting_app.orchestrator.gateway.grant(waiting.task, action, cio.as_actor())
    assert granted.status == "APPROVED"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(waiting_app, ResponsibilitySource.ACTION, granted.id, target)
    executing = waiting_app.runtime.repos.actions.save(
        waiting_app.engine.fire(
            granted,
            "execute",
            actor=owner.as_actor(),
            reason_code="ROUTINE_PROGRESSION",
            reason="sandbox execution started",
            correlation_id=waiting.task.id,
        ).entity
    )
    assert executing.status == "EXECUTING"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(waiting_app, ResponsibilitySource.ACTION, executing.id, target)
    executed = waiting_app.runtime.repos.actions.save(
        waiting_app.engine.fire(
            executing,
            "executed",
            actor=owner.as_actor(),
            reason_code="ROUTINE_PROGRESSION",
            reason="sandbox execution returned",
            correlation_id=waiting.task.id,
        ).entity
    )
    assert executed.status == "EXECUTED"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(waiting_app, ResponsibilitySource.ACTION, executed.id, target)
    fresh_root = tmp_path / "settled"
    fresh_root.mkdir()
    (fresh_root / "ledger.json").write_text(
        json.dumps({"value": 0, "applied": []}), encoding="utf-8"
    )
    fresh = build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(FrozenClock(start), world_effect=True),
        clock=FrozenClock(start),
        world_root=fresh_root,
        config=staffed_config(),
    )
    fresh_wait = fresh.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    done = fresh.orchestrator.grant_approval(
        fresh_wait.task.id, granted_by_agent_id=_human(fresh).id
    )
    settled_action = done.actions[0]
    assert settled_action.status == "SETTLED"
    with pytest.raises(QORError, match="cannot take a responsibility"):
        _bind(fresh, ResponsibilitySource.ACTION, settled_action.id, _other_human(fresh).id)


def test_t12_only_one_active_responsibility(clock):
    app, result = _wait(clock)
    first = _other_human(app)
    _bind(app, ResponsibilitySource.TASK, result.task.id, first.id)
    second = _other_human(app)
    with pytest.raises(QORError, match="ACTIVE"):
        _bind(app, ResponsibilitySource.TASK, result.task.id, second.id)
    active = [
        item
        for item in app.runtime.repos.responsibilities.list_by_source(
            ResponsibilitySource.TASK, result.task.id
        )
        if item.status == "ACTIVE"
    ]
    assert len(active) == 1


def test_t13_repeat_create_does_not_add_another_active_row(clock):
    app, result = _wait(clock)
    human = _other_human(app)
    first = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    second = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    assert second.id == first.id
    assert (
        len(
            app.runtime.repos.responsibilities.list_by_source(
                ResponsibilitySource.TASK, result.task.id
            )
        )
        == 1
    )


def _transfer(app, result):
    first_human = _other_human(app)
    second_human = _other_human(app)
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, first_human.id)
    moved = app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=second_human.id,
        reason="the first human is unavailable",
    )
    return original, moved, first_human, second_human


def test_t14_human_cio_can_transfer(clock):
    app, result = _wait(clock)
    original, moved, _first, second = _transfer(app, result)
    assert moved.id != original.id
    assert moved.responsible_human_agent_id == second.id


def test_t16_transfer_marks_the_old_record_transferred(clock):
    app, result = _wait(clock)
    original, _moved, first, _second = _transfer(app, result)
    old = app.runtime.repos.responsibilities.get(original.id)
    assert old.status == "TRANSFERRED"
    assert old.responsible_human_agent_id == first.id


def test_t17_transfer_creates_a_new_active_record(clock):
    app, result = _wait(clock)
    original, moved, _first, second = _transfer(app, result)
    assert moved.status == "ACTIVE"
    assert moved.responsible_human_agent_id == second.id
    assert moved.transferred_from_responsibility_id == original.id
    assert moved.transfer_reason == "the first human is unavailable"


def test_t18_transfer_leaves_one_active_responsibility(clock):
    app, result = _wait(clock)
    _original, moved, _first, _second = _transfer(app, result)
    active = [
        item
        for item in app.runtime.repos.responsibilities.list_by_source(
            ResponsibilitySource.TASK, result.task.id
        )
        if item.status == "ACTIVE"
    ]
    assert len(active) == 1
    assert active[0].id == moved.id


def test_t19_transfer_requires_a_reason(clock):
    app, result = _wait(clock)
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    with pytest.raises(QORError, match="reason"):
        app.orchestrator.responsibilities.transfer_responsibility(
            original.id,
            caller_agent_id=_human(app).id,
            responsible_human_agent_id=_human(app).id,
            reason="   ",
        )
    assert app.runtime.repos.responsibilities.get(original.id).status == "ACTIVE"


def test_t15_non_cio_cannot_transfer(clock):
    app, result = _wait(clock)
    first_human = _other_human(app)
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, first_human.id)
    with pytest.raises(QORError, match="human CIO"):
        app.orchestrator.responsibilities.transfer_responsibility(
            original.id,
            caller_agent_id=first_human.id,
            responsible_human_agent_id=_human(app).id,
            reason="the target wants to take it",
        )


def test_t20_human_cio_can_clear(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    cleared = app.orchestrator.responsibilities.clear_responsibility(
        created.id, caller_agent_id=_human(app).id, reason="the wait was withdrawn"
    )
    assert cleared.status == "CLEARED"
    assert cleared.cleared_by_agent_id == _human(app).id
    assert cleared.cleared_at is not None


def test_t21_non_cio_cannot_clear(clock):
    app, result = _wait(clock)
    other = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    with pytest.raises(QORError, match="human CIO"):
        app.orchestrator.responsibilities.clear_responsibility(
            created.id, caller_agent_id=other.id, reason="not my job"
        )
    assert app.runtime.repos.responsibilities.get(created.id).status == "ACTIVE"


def test_t22_clear_requires_a_reason(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    with pytest.raises(QORError, match="reason"):
        app.orchestrator.responsibilities.clear_responsibility(
            created.id, caller_agent_id=_human(app).id, reason=" "
        )
    assert app.runtime.repos.responsibilities.get(created.id).status == "ACTIVE"


def test_t23_clear_keeps_the_human_and_ends_the_record(clock):
    app, result = _wait(clock)
    other = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    cleared = app.orchestrator.responsibilities.clear_responsibility(
        created.id, caller_agent_id=_human(app).id, reason="the wait was withdrawn"
    )
    assert cleared.status == "CLEARED"
    assert cleared.clear_reason == "the wait was withdrawn"
    assert cleared.responsible_human_agent_id == other.id
    assert app.runtime.repos.tasks.get(result.task.id).status == "AWAITING_HUMAN"


def test_t24_source_completion_completes_the_responsibility(clock):
    app, result = _wait(clock)
    human = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, human.id)
    action = _bind(app, ResponsibilitySource.ACTION, result.actions[0].id, human.id)
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    finished = app.runtime.repos.responsibilities.get(created.id)
    finished_action = app.runtime.repos.responsibilities.get(action.id)
    assert finished.status == "COMPLETED"
    assert finished.completed_at is not None
    assert finished.responsible_human_agent_id == created.responsible_human_agent_id
    assert finished_action.status == "COMPLETED"
    assert finished_action.completed_at is not None


def test_t25_completed_cannot_become_active(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    finished = app.runtime.repos.responsibilities.get(created.id)
    revived = finished.model_copy(update={"status": "ACTIVE"})
    with pytest.raises(IllegalStateWrite):
        app.runtime.repos.responsibilities.save(revived)


def test_t26_cleared_cannot_become_active(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    cleared = app.orchestrator.responsibilities.clear_responsibility(
        created.id, caller_agent_id=_human(app).id, reason="withdrawn"
    )
    revived = cleared.model_copy(update={"status": "ACTIVE"})
    with pytest.raises(IllegalStateWrite):
        app.runtime.repos.responsibilities.save(revived)


def test_t27_transferred_cannot_become_active(clock):
    app, result = _wait(clock)
    original = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    app.orchestrator.responsibilities.transfer_responsibility(
        original.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=_human(app).id,
        reason="hand to the CIO explicitly",
    )
    old = app.runtime.repos.responsibilities.get(original.id)
    revived = old.model_copy(update={"status": "ACTIVE"})
    with pytest.raises(IllegalStateWrite):
        app.runtime.repos.responsibilities.save(revived)


def test_t28_responsibility_does_not_change_source_state(clock):
    app, result = _wait(clock)
    other = _other_human(app)
    action = result.actions[0]
    _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    _bind(app, ResponsibilitySource.ACTION, action.id, other.id)
    assert app.runtime.repos.tasks.get(result.task.id).status == "AWAITING_HUMAN"
    assert app.runtime.repos.actions.get(action.id).status == "APPROVAL_PENDING"


def _watch(obj, name: str, calls: dict[str, int]) -> None:
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        calls[name] = calls.get(name, 0) + 1
        return original(*args, **kwargs)

    setattr(obj, name, wrapped)


def _exercise_service(app, result) -> None:
    other = _other_human(app)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    moved = app.orchestrator.responsibilities.transfer_responsibility(
        created.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=_human(app).id,
        reason="explicit handoff",
    )
    app.orchestrator.responsibilities.clear_responsibility(
        moved.id, caller_agent_id=_human(app).id, reason="no longer required"
    )
    rebound = _bind(app, ResponsibilitySource.TASK, result.task.id, other.id)
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    assert app.runtime.repos.responsibilities.get(rebound.id).status == "COMPLETED"


@pytest.mark.parametrize(
    ("number", "owner_name", "method"),
    [
        (29, "planner", "plan"),
        (30, "agents", "execute_work"),
        (31, "reviewing", "review"),
        (32, "deciding", "decide"),
        (33, "gateway", "request"),
        (34, "scheduling", "tick"),
        (35, "routing", "route"),
    ],
)
def test_t29_to_t35_service_does_not_call_downstream(clock, number, owner_name, method):
    app, result = _wait(clock)
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
    _exercise_service(app, result)
    assert calls.get(method, 0) == 0, f"T{number} observed {method}"


def test_t36_audit_contains_create(clock):
    app, result = _wait(clock)
    _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    assert _audits(app, "CREATE")


def test_t37_audit_contains_transfer(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    app.orchestrator.responsibilities.transfer_responsibility(
        created.id,
        caller_agent_id=_human(app).id,
        responsible_human_agent_id=_human(app).id,
        reason="explicit handoff",
    )
    assert _audits(app, "TRANSFER")


def test_t38_audit_contains_clear(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    app.orchestrator.responsibilities.clear_responsibility(
        created.id, caller_agent_id=_human(app).id, reason="no longer required"
    )
    assert _audits(app, "CLEAR")


def test_t39_audit_contains_complete(clock):
    app, result = _wait(clock)
    created = _bind(app, ResponsibilitySource.TASK, result.task.id, _other_human(app).id)
    app.orchestrator.grant_approval(result.task.id, granted_by_agent_id=_human(app).id)
    assert app.runtime.repos.responsibilities.get(created.id).status == "COMPLETED"
    assert _audits(app, "COMPLETE")


def test_t40_scheduler_does_not_create_responsibility(clock):
    from datetime import UTC, datetime

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
    assert app.runtime.repos.responsibilities.all() == ()
    opened = next(
        task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.SCHEDULE
    )
    assert opened.origin is TaskOrigin.SCHEDULE


def test_t41_event_router_does_not_create_responsibility(clock):
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
    assert app.runtime.repos.responsibilities.all() == ()
    opened = next(task for task in app.runtime.repos.tasks.all() if task.origin is TaskOrigin.EVENT)
    assert opened.origin is TaskOrigin.EVENT


def test_decision_source_is_not_implemented():
    with pytest.raises(ValueError):
        ResponsibilitySource("DECISION")
