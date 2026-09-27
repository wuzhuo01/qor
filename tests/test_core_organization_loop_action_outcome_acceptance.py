"""One generic task from decision through sandbox verification into the next plan.

Planner and agent runtime are ports. Action gating, world write, independent
reread, outcome record and the next PlanRequest are production services.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from qor.app import build_app
from qor.domain.enums import ArtifactType
from qor.kernel.errors import QORError
from qor.ports.agent_runtime import ActionProposal, DecisionProposal, DecisionRequest
from tests.generic_staff import staffed_config
from tests.test_core_organization_loop_acceptance import GenericOrgAgents, GenericOrgPlanner

OBJECTIVE = (
    "Prepare a routine organizational configuration update, decide the approved change, "
    "apply it in the sandbox, and verify the resulting world state."
)
FOLLOW = (
    "Plan the next cycle using the recorded organizational configuration outcome, "
    "without re-running the update that already settled."
)
SUBJECT = "organizational configuration"
RESOURCE = "org-configuration"


class ConfigurationAgents(GenericOrgAgents):
    def propose_decision(self, agent_id, request: DecisionRequest) -> DecisionProposal:
        self.decision_requests.append(request)
        environment = request.permitted_environments[0]
        return DecisionProposal(
            decision_type="PARAMETER_CHANGE",
            statement="Apply the approved organizational configuration update in the sandbox.",
            rationale=(
                "The accepted account names one configuration change. "
                f"Task context: {request.task_objective}"
            ),
            alternatives=(
                {
                    "option": "Leave the configuration unchanged",
                    "rejected_because": "the accepted account already selected a change",
                },
            ),
            metrics=("configuration_counter",),
            evaluate_after="the next organizational cycle",
            success_criteria=("the sandbox counter matches the approved increment",),
            proposed_actions=(
                ActionProposal(
                    system="sandbox-world",
                    resource=RESOURCE,
                    environment=environment,
                    operation="increment",
                    capability_key="sandbox.world_increment",
                    payload={"delta": 1},
                    is_reversible=True,
                    dry_run=False,
                ),
            ),
        )


class _CountingWriter:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, int, str]] = []

    def apply(self, *, resource: str, delta: int, idempotency_key: str) -> None:
        self.calls.append((resource, delta, idempotency_key))
        self._inner.apply(resource=resource, delta=delta, idempotency_key=idempotency_key)


class _CountingReader:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def read(self, resource: str) -> dict[str, str] | None:
        observed = self._inner.read(resource)
        self.calls.append((resource, observed))
        return observed


def _human(app):
    return app.runtime.repos.agents.get(
        app.runtime.agent_index[app.runtime.config.organization["human_cio"]]
    )


def _seed(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{RESOURCE}.json").write_text(
        json.dumps({"value": 0, "applied": []}),
        encoding="utf-8",
    )


def test_core_organization_loop_reaches_verified_outcome_and_is_read_by_next_plan(clock, tmp_path):
    _seed(tmp_path)
    planner = GenericOrgPlanner()
    agents = ConfigurationAgents(clock)
    app = build_app(
        planner=planner, agents=agents, clock=clock, world_root=tmp_path, config=staffed_config()
    )
    gateway = app.orchestrator.gateway
    writer = _CountingWriter(gateway._world_writer)
    reader = _CountingReader(gateway._world_reader)
    gateway._world_writer = writer
    gateway._world_reader = reader
    deliver_calls: list[str] = []
    original_deliver = gateway.deliver

    def _deliver(task, action):
        deliver_calls.append(action.ref)
        return original_deliver(task, action)

    gateway.deliver = _deliver  # type: ignore[method-assign]
    record_calls: list[object] = []
    original_record = app.orchestrator.outcomes.record

    def _record(**kwargs):
        record_calls.append(kwargs.get("recorded_by_agent_id"))
        return original_record(**kwargs)

    app.orchestrator.outcomes.record = _record  # type: ignore[method-assign]

    result = app.orchestrator.run_objective(OBJECTIVE)
    task = result.task
    decision = result.decision
    assert task.status == "COMPLETED"
    assert decision is not None
    assert decision.status == "DECIDED"
    assert planner.calls == 1
    assert planner.last_request is not None
    assert planner.last_request.prior_outcomes == ()
    assert agents.work_requests and agents.review_requests and agents.decision_requests
    assert task.active_plan_id is not None
    plan = app.runtime.repos.plans.get(task.active_plan_id)
    assert plan.status == "ACTIVE"
    assert any(app.runtime.repos.assignments.all())
    assert any(item.task_id == task.id for item in app.runtime.repos.reviews.all())

    assert len(result.actions) == 1
    action = app.runtime.repos.actions.get(result.actions[0].id)
    assert action.decision_id == decision.id
    assert action.dry_run is False
    assert action.target.environment.value == "SANDBOX"
    assert {item.kind: item.verdict for item in action.checks} == {
        "PERMISSION": "PASSED",
        "RISK": "PASSED",
        "APPROVAL": "WAIVED",
    }
    assert deliver_calls == [action.ref]
    assert [item[0] for item in writer.calls] == [RESOURCE]
    assert [item[0] for item in reader.calls] == [RESOURCE, RESOURCE]
    assert reader.calls[0][1] == {"value": "0"}
    assert writer.calls[0][1] == 1
    assert reader.calls[1][1] == {"value": "1"}
    verification = action.verification
    assert verification is not None
    assert verification.expected == {"value": "1"}
    assert verification.observed == {"value": "1"}
    assert verification.matched is True
    assert action.status == "SETTLED"
    on_disk = json.loads((tmp_path / f"{RESOURCE}.json").read_text(encoding="utf-8"))
    assert on_disk["value"] == 1
    assert app.runtime.repos.outcomes.all() == ()

    human = _human(app)
    metrics = decision.measurement_plan.metrics[:1]
    rejected = {
        "decision_id": decision.id,
        "subject_key": SUBJECT,
        "verdict": "CONFIRMED",
        "contradicted_decision_type": decision.decision_type.value,
        "confidence": "HIGH",
        "statement": "The sandbox configuration counter matched the approved increment.",
        "recorded_by_agent_id": human.id,
        "measurement_metrics": metrics,
    }
    with pytest.raises(QORError):
        app.orchestrator.outcomes.record(**rejected)
    with pytest.raises(QORError):
        app.orchestrator.outcomes.record(**rejected, source_action_id=uuid4())

    outcome = app.orchestrator.outcomes.record(**rejected, source_action_id=action.id)
    assert record_calls == [human.id, human.id, human.id]
    assert len(app.runtime.repos.outcomes.all()) == 1
    assert outcome.status == "AUTHORITATIVE"
    assert outcome.source_action_id == action.id
    assert outcome.recorded_by_agent_id == human.id
    assert outcome.created_by.id == human.id
    cited = app.runtime.repos.actions.get(outcome.source_action_id)
    assert cited.verification is not None
    assert cited.verification.matched is True
    assert cited.verification.observed == verification.observed

    follow = app.orchestrator.intake.submit(
        objective=FOLLOW,
        requested_by_agent_id=human.id,
        required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
    )
    app.orchestrator.planning.plan(follow)
    request = planner.last_request
    assert planner.calls == 2
    assert request is not None
    assert request.task_ref == follow.ref
    facts = [item for item in request.prior_outcomes if item.ref == outcome.ref]
    assert len(facts) == 1
    fact = facts[0]
    assert fact.decision_ref == decision.ref
    assert fact.subject_key == SUBJECT
    assert fact.verdict == "CONFIRMED"
    assert fact.status == "AUTHORITATIVE"
    assert fact.statement == outcome.statement
    assert all(item.outcome_ref != outcome.ref for item in request.binding_constraints)
    assert app.runtime.repos.outcomes.get(outcome.id).source_action_id == action.id
    assert follow.id != task.id
