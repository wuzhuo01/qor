"""Task ESCALATED contract: human-wait escalation only.

Kill-mock uses real Task transitions. Source lists and service source text are not evidence.
"""

from __future__ import annotations

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import RiskLevel
from qor.kernel.errors import TransitionRejected
from tests.contract_agents import ContractAgentRuntime
from tests.generic_staff import staffed_config

OBJECTIVE = "Prepare the organizational status for this cycle."


def _app(clock):
    return build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(clock),
        clock=clock,
        config=staffed_config(),
    )


def _human(app):
    return app.runtime.repos.agents.get(
        app.runtime.agent_index[app.runtime.config.organization["human_cio"]]
    )


def _open(app):
    return app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        requested_by_agent_id=_human(app).id,
    )


def _fire(app, task, name: str, *, reason_code: str, reason: str):
    actor = _human(app).as_actor()
    return app.runtime.repos.tasks.save(
        app.engine.fire(
            task,
            name,
            actor=actor,
            reason_code=reason_code,
            reason=reason,
            correlation_id=task.id,
        ).entity
    )


def _reject_escalate(app, task) -> None:
    status = task.status
    with pytest.raises(TransitionRejected) as caught:
        _fire(
            app,
            task,
            "escalate",
            reason_code="HUMAN_WAIT_ESCALATION",
            reason="illegal task escalation source",
        )
    assert caught.value.transition == "escalate"
    assert app.runtime.repos.tasks.get(task.id).status == status


def test_human_wait_escalation_round_trip(clock):
    app = _app(clock)
    waiting = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    assert waiting.task.status == "AWAITING_HUMAN"
    escalated = app.orchestrator.escalate_task(
        waiting.task.id, escalated_by_agent_id=_human(app).id
    )
    assert escalated.task.status == "ESCALATED"
    resolved = app.orchestrator.resolve_escalation(
        escalated.task.id, resolved_by_agent_id=_human(app).id
    )
    assert resolved.task.status == "AWAITING_HUMAN"
    hops = [
        item
        for item in app.runtime.log.transitions_for(waiting.task.id)
        if item.entity_type == "Task" and item.transition in {"escalate", "resolve_escalation"}
    ]
    assert [(item.transition, item.from_state, item.to_state) for item in hops] == [
        ("escalate", "AWAITING_HUMAN", "ESCALATED"),
        ("resolve_escalation", "ESCALATED", "AWAITING_HUMAN"),
    ]


def test_open_cannot_escalate(clock):
    app = _app(clock)
    _reject_escalate(app, _open(app))


def test_running_cannot_escalate(clock):
    app = _app(clock)
    ready, _plan = app.orchestrator.planning.plan(_open(app))
    running = _fire(
        app,
        ready,
        "start",
        reason_code="ROUTINE_PROGRESSION",
        reason="execution started",
    )
    assert running.status == "RUNNING"
    _reject_escalate(app, running)


def test_blocked_cannot_escalate(clock):
    app = _app(clock)
    blocked = _fire(
        app,
        _open(app),
        "block",
        reason_code="CAPABILITY_GAP",
        reason="blocked before execution",
    )
    assert blocked.status == "BLOCKED"
    _reject_escalate(app, blocked)
