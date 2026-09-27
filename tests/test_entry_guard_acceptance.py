"""Task Execution Entry Guard v0.1. Policy v0.2 is frozen and not restated here.

Kill-mock for DRAFT, PLANNING, RUNNING, AWAITING_HUMAN, COMPLETED, FAILED, CANCELLED:
the rejection happens before another planner call, and plan/node/work/action counts
do not grow on that call.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import RiskLevel, TaskOrigin
from qor.domain.task import NodeKind, Task
from qor.kernel.clock import FrozenClock
from qor.kernel.errors import EntryRejected
from qor.services.orchestrator import RunResult
from tests.contract_agents import ContractAgentRuntime
from tests.generic_staff import staffed_config

pytestmark = pytest.mark.qor_entry_guard

WHEN = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
OBJECTIVE = "Prepare the organizational status for this cycle."


class CountingPlanner:
    def __init__(self, inner: StubPlanner) -> None:
        self._inner = inner
        self.calls = 0
        self.kind = inner.kind
        self.provider = inner.provider

    def plan(self, request):
        self.calls += 1
        return self._inner.plan(request)


class CountingAgents:
    def __init__(self, inner: ContractAgentRuntime) -> None:
        self._inner = inner
        self.work_calls = 0

    def execute_work(self, agent_id, request):
        self.work_calls += 1
        return self._inner.execute_work(agent_id, request)

    def execute_review(self, agent_id, request):
        return self._inner.execute_review(agent_id, request)

    def propose_decision(self, agent_id, request):
        return self._inner.propose_decision(agent_id, request)


def _world(clock: FrozenClock):
    planner = CountingPlanner(StubPlanner())
    agents = CountingAgents(ContractAgentRuntime(clock))
    app = build_app(planner=planner, agents=agents, clock=clock, config=staffed_config())
    return app, planner, agents


def _human(app):
    return app.runtime.repos.agents.get(
        app.runtime.agent_index[app.runtime.config.organization["human_cio"]]
    )


def _owner(app, task: Task):
    return app.runtime.repos.agents.get(task.owner_agent_id)


def _open(app) -> Task:
    return app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        requested_by_agent_id=_human(app).id,
    )


def _draft(app) -> Task:
    human = _human(app)
    now = app.runtime.clock.now()
    tid, ref = app.runtime.mint("TASK")
    task = Task(
        id=tid,
        ref=ref,
        status="DRAFT",
        created_at=now,
        created_by=human.as_actor(),
        updated_at=now,
        organization_id=app.runtime.organization().id,
        title=OBJECTIVE[:80],
        objective=OBJECTIVE,
        origin=TaskOrigin.HUMAN,
        requested_by=human.id,
        owner_agent_id=_owner_id(app),
        max_plan_revisions=app.runtime.policies.max_plan_revisions(),
    )
    return app.runtime.repos.tasks.insert(task)


def _owner_id(app):
    return app.runtime.agent_index[app.runtime.config.organization["default_task_owner"]]


def _fire(app, task: Task, name: str, *, reason_code: str, reason: str, actor=None) -> Task:
    who = actor or _owner(app, task).as_actor()
    return app.runtime.repos.tasks.save(
        app.engine.fire(
            task,
            name,
            actor=who,
            reason_code=reason_code,
            reason=reason,
            correlation_id=task.id,
        ).entity
    )


def _ready(app, task: Task) -> tuple[Task, object]:
    return app.orchestrator.planning.plan(task)


def _running(app, task: Task) -> Task:
    ready, _plan = _ready(app, task)
    return _fire(
        app,
        ready,
        "start",
        reason_code="ROUTINE_PROGRESSION",
        reason="execution started",
    )


def _counts(app, planner: CountingPlanner, agents: CountingAgents) -> tuple[int, ...]:
    return (
        planner.calls,
        len(app.runtime.repos.plans.all()),
        len(app.runtime.repos.nodes.all()),
        agents.work_calls,
        len(app.runtime.repos.actions.all()),
    )


def _reject(app, task: Task, planner: CountingPlanner, agents: CountingAgents) -> EntryRejected:
    before = _counts(app, planner, agents)
    status = task.status
    with pytest.raises(EntryRejected) as caught:
        app.orchestrator.run_task(task)
    assert caught.value.task_id == task.id
    assert caught.value.state == status
    assert _counts(app, planner, agents) == before
    assert app.runtime.repos.tasks.get(task.id).status == status
    return caught.value


def test_t1_open_allows_first_execution(clock):
    app, planner, agents = _world(clock)
    task = _open(app)
    result = app.orchestrator.run_task(task)
    assert planner.calls >= 1
    assert result.task.status == "COMPLETED"
    assert agents.work_calls > 0


def test_t2_ready_with_plan_resumes_without_replanning(clock):
    app, planner, _agents = _world(clock)
    task = _open(app)
    ready, plan = _ready(app, task)
    assert ready.status == "READY"
    calls = planner.calls
    plans = len(app.runtime.repos.plans.all())
    nodes = len(app.runtime.repos.nodes.all())
    result = app.orchestrator.run_task(ready)
    assert planner.calls == calls
    assert len(app.runtime.repos.plans.all()) == plans
    assert len(app.runtime.repos.nodes.all()) == nodes
    assert app.runtime.repos.plans.get(plan.id).id == plan.id
    assert result.task.status == "COMPLETED"


def test_t3_ready_without_plan_is_invalid(clock):
    app, planner, agents = _world(clock)
    ready, _plan = _ready(app, _open(app))
    bare = app.runtime.repos.tasks.save(ready.model_copy(update={"active_plan_id": None}))
    assert bare.status == "READY"
    assert bare.active_plan_id is None
    rejected = _reject(app, bare, planner, agents)
    assert rejected.reason == "INVALID_STATE"


def test_t4_draft_rejected_before_planning(clock):
    app, planner, agents = _world(clock)
    rejected = _reject(app, _draft(app), planner, agents)
    assert rejected.reason == "REJECT"
    assert rejected.state == "DRAFT"


def test_t5_planning_rejected_before_another_plan(clock):
    app, planner, agents = _world(clock)
    planning = _fire(
        app,
        _open(app),
        "start_planning",
        reason_code="ROUTINE_PROGRESSION",
        reason="planning began",
    )
    assert planning.status == "PLANNING"
    rejected = _reject(app, planning, planner, agents)
    assert rejected.reason == "REJECT"


def test_t6_running_rejected_without_block(clock):
    app, planner, agents = _world(clock)
    running = _running(app, _open(app))
    assert running.status == "RUNNING"
    _reject(app, running, planner, agents)
    assert app.runtime.repos.tasks.get(running.id).status == "RUNNING"


def test_t7_review_rejected(clock):
    app, planner, agents = _world(clock)
    running = _running(app, _open(app))
    plan = app.runtime.repos.plans.get(running.active_plan_id)
    app.orchestrator._run_phase(running, plan, NodeKind.WORK, RunResult(task=running))
    review = _fire(
        app,
        app.runtime.repos.tasks.get(running.id),
        "nodes_submitted",
        reason_code="WORK_SUBMITTED",
        reason="work submitted",
    )
    assert review.status == "REVIEW"
    _reject(app, review, planner, agents)


def test_t8_synthesizing_rejected(clock):
    app, planner, agents = _world(clock)
    review = _to_review(app)
    synthesizing = _fire(
        app,
        review,
        "reviews_passed",
        reason_code="REVIEW_PASSED",
        reason="reviews resolved",
    )
    assert synthesizing.status == "SYNTHESIZING"
    _reject(app, synthesizing, planner, agents)


def test_t9_deciding_rejected(clock):
    app, planner, agents = _world(clock)
    review = _to_review(app)
    synthesizing = _fire(
        app,
        review,
        "reviews_passed",
        reason_code="REVIEW_PASSED",
        reason="reviews resolved",
    )
    plan = app.runtime.repos.plans.get(synthesizing.active_plan_id)
    app.orchestrator._run_phase(
        synthesizing, plan, NodeKind.SYNTHESIS, RunResult(task=synthesizing)
    )
    deciding = _fire(
        app,
        app.runtime.repos.tasks.get(synthesizing.id),
        "synthesis_ready",
        reason_code="SYNTHESIS_READY",
        reason="synthesis accepted",
    )
    assert deciding.status == "DECIDING"
    _reject(app, deciding, planner, agents)


def test_t10_awaiting_human_rejected(clock):
    app, planner, agents = _world(clock)
    waiting = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    assert waiting.task.status == "AWAITING_HUMAN"
    _reject(app, waiting.task, planner, agents)
    assert app.runtime.repos.tasks.get(waiting.task.id).status == "AWAITING_HUMAN"


def test_t11_action_pending_rejected(clock):
    app, planner, agents = _world(clock)
    waiting = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    actor = _human(app).as_actor()
    for action in waiting.actions:
        app.orchestrator.gateway.grant(waiting.task, action, actor)
    pending = _fire(
        app,
        app.runtime.repos.tasks.get(waiting.task.id),
        "human_granted",
        reason_code="APPROVAL_GRANTED",
        reason="grant recorded without resuming the task",
        actor=actor,
    )
    assert pending.status == "ACTION_PENDING"
    _reject(app, pending, planner, agents)


def test_t12_blocked_is_not_unblocked(clock):
    app, planner, agents = _world(clock)
    blocked = _fire(
        app,
        _open(app),
        "block",
        reason_code="CAPABILITY_GAP",
        reason="blocked before execution",
    )
    assert blocked.status == "BLOCKED"
    _reject(app, blocked, planner, agents)


def test_t13_escalated_is_not_resolved(clock):
    app, planner, agents = _world(clock)
    waiting = app.orchestrator.run_objective(OBJECTIVE, risk_level=RiskLevel.HIGH)
    assert waiting.task.status == "AWAITING_HUMAN"
    escalated = app.orchestrator.escalate_task(
        waiting.task.id, escalated_by_agent_id=_human(app).id
    )
    assert escalated.task.status == "ESCALATED"
    _reject(app, escalated.task, planner, agents)


def test_t14_completed_rejected_before_planning(clock):
    app, planner, agents = _world(clock)
    done = app.orchestrator.run_objective(OBJECTIVE)
    assert done.task.status == "COMPLETED"
    _reject(app, done.task, planner, agents)


def test_t15_failed_is_not_retried(clock):
    app, planner, agents = _world(clock)
    failed = _fire(
        app,
        _open(app),
        "fail",
        reason_code="UNRECOVERABLE_ERROR",
        reason="failed before execution",
    )
    assert failed.status == "FAILED"
    _reject(app, failed, planner, agents)


def test_t16_cancelled_rejected(clock):
    app, planner, agents = _world(clock)
    cancelled = _fire(
        app,
        _open(app),
        "cancel",
        reason_code="CANCELLED_BY_REQUESTER",
        reason="cancelled before execution",
        actor=_human(app).as_actor(),
    )
    assert cancelled.status == "CANCELLED"
    _reject(app, cancelled, planner, agents)


def _to_review(app) -> Task:
    running = _running(app, _open(app))
    plan = app.runtime.repos.plans.get(running.active_plan_id)
    app.orchestrator._run_phase(running, plan, NodeKind.WORK, RunResult(task=running))
    return _fire(
        app,
        app.runtime.repos.tasks.get(running.id),
        "nodes_submitted",
        reason_code="WORK_SUBMITTED",
        reason="work submitted",
    )
