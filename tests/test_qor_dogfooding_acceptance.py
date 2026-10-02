"""Three generic tasks through the existing organization runtime.

The planner is the production stub. It sees capabilities, not agents.
The agent port only returns work, review, and a decision proposal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import build_app
from qor.domain.enums import ArtifactType
from qor.kernel.errors import QORError
from qor.ports.agent_runtime import ActionProposal, DecisionProposal, DecisionRequest
from qor.ports.planner import PlanRequest
from tests.generic_staff import staffed_config
from tests.test_core_organization_loop_acceptance import GenericOrgAgents

ANALYZE = (
    "Collect the current organizational status, analyze what should stay, "
    "and recommend what the next cycle should keep."
)
APPLY = (
    "Prepare a routine organizational configuration update, decide the approved change, "
    "apply it in the sandbox, and verify the resulting world state."
)
FOLLOW = (
    "Decide what the next cycle should do with the recorded organizational configuration outcome."
)
SUBJECT = "organizational configuration"
RESOURCE = "org-configuration"


class RecordingStub(StubPlanner):
    def __init__(self) -> None:
        self.requests: list[PlanRequest] = []

    def plan(self, request: PlanRequest):
        self.requests.append(request)
        return super().plan(request)


class DogfoodAgents(GenericOrgAgents):
    def propose_decision(self, agent_id, request: DecisionRequest) -> DecisionProposal:
        self.decision_requests.append(request)
        if "apply it in the sandbox" in request.task_objective:
            return DecisionProposal(
                decision_type="PARAMETER_CHANGE",
                statement="Apply the approved organizational configuration update in the sandbox.",
                rationale=f"The accepted account selected one change. Task context: {request.task_objective}",
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
                        environment=request.permitted_environments[0],
                        operation="increment",
                        capability_key="sandbox.world_increment",
                        payload={"delta": 1},
                        is_reversible=True,
                        dry_run=False,
                    ),
                ),
            )
        return DecisionProposal(
            decision_type="NO_ACTION",
            statement="No further organizational action is required.",
            rationale=f"The accepted account is enough. Task context: {request.task_objective}",
            alternatives=(
                {
                    "option": "Open another work cycle immediately",
                    "rejected_because": "the current objective is already covered",
                },
            ),
            metrics=("cycle_account_accepted",),
            evaluate_after="the next organizational cycle",
            success_criteria=("the accepted account remains the cycle record",),
            proposed_actions=(),
        )


def _human(app):
    return app.runtime.repos.agents.get(
        app.runtime.agent_index[app.runtime.config.organization["human_cio"]]
    )


def _request(planner: RecordingStub, task_ref: str) -> PlanRequest:
    matches = [item for item in planner.requests if item.task_ref == task_ref]
    assert matches
    return matches[-1]


def _nodes(app, task):
    plan = app.runtime.repos.plans.get(task.active_plan_id)
    return plan, [app.runtime.repos.nodes.get(node_id) for node_id in plan.node_ids]


def test_three_generic_organization_tasks_can_be_run(clock, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{RESOURCE}.json").write_text(
        json.dumps({"value": 0, "applied": []}),
        encoding="utf-8",
    )
    planner = RecordingStub()
    agents = DogfoodAgents(clock)
    app = build_app(
        planner=planner,
        agents=agents,
        clock=clock,
        world_root=tmp_path,
        config=staffed_config(),
    )

    analyzed = app.orchestrator.run_objective(ANALYZE)
    applied = app.orchestrator.run_objective(APPLY)

    assert app.runtime.repos.outcomes.all() == ()
    human = _human(app)
    decision = applied.decision
    assert decision is not None and decision.measurement_plan is not None
    action = app.runtime.repos.actions.get(applied.actions[0].id)
    rejected = {
        "decision_id": decision.id,
        "subject_key": SUBJECT,
        "verdict": "CONFIRMED",
        "contradicted_decision_type": decision.decision_type.value,
        "confidence": "HIGH",
        "statement": "The sandbox configuration counter matched the approved increment.",
        "recorded_by_agent_id": human.id,
        "measurement_metrics": decision.measurement_plan.metrics[:1],
    }
    with pytest.raises(QORError):
        app.orchestrator.outcomes.record(**rejected)
    outcome = app.orchestrator.outcomes.record(**rejected, source_action_id=action.id)

    followed = app.orchestrator.run_objective(FOLLOW)

    for result, objective in (
        (analyzed, ANALYZE),
        (applied, APPLY),
        (followed, FOLLOW),
    ):
        task = result.task
        assert task.status == "COMPLETED"
        assert task.objective == objective
        plan, nodes = _nodes(app, task)
        assert plan.status == "ACTIVE"
        request = _request(planner, task.ref)
        assert "agent_id" not in request.model_dump_json()
        assert objective in request.objective
        assert any(objective in node.objective for node in nodes)
        capabilities = {node.required_capabilities for node in nodes}
        assert len(capabilities) >= 3
        assignments = [
            item for item in app.runtime.repos.assignments.all() if item.task_id == task.id
        ]
        assert len({item.assignee_agent_id for item in assignments}) >= 2
        artifacts = [item for item in app.runtime.repos.artifacts.all() if item.task_id == task.id]
        assert artifacts
        assert any(item.artifact_type == ArtifactType.SYNTHESIS for item in artifacts)
        reviews = [item for item in app.runtime.repos.reviews.all() if item.task_id == task.id]
        assert reviews
        assert all(item.status == "PASSED" for item in reviews)
        assert result.decision is not None
        assert result.decision.status == "DECIDED"
        assert result.decision.based_on_artifact_ids
        assert result.decision.based_on_review_ids

    synthesis = next(
        item
        for item in agents.work_requests
        if item.task_ref == analyzed.task.ref
        and item.expected_artifact_type == ArtifactType.SYNTHESIS
    )
    assert synthesis.input_artifacts
    assert analyzed.decision is not None
    assert analyzed.decision.decision_type.value == "NO_ACTION"
    assert analyzed.actions == ()

    assert action.decision_id == applied.decision.id
    assert action.status == "SETTLED"
    assert action.verification is not None
    assert action.verification.expected == action.verification.observed == {"value": "1"}
    assert action.verification.matched is True
    observed = [
        event
        for event in app.runtime.log.events()
        if event.name == "qor.action.observed" and event.correlation_id == applied.task.id
    ]
    assert observed[-1].payload["matched"] is True
    assert json.loads((tmp_path / f"{RESOURCE}.json").read_text(encoding="utf-8"))["value"] == 1
    assert outcome.source_action_id == action.id
    assert outcome.recorded_by_agent_id == human.id
    assert outcome.status == "AUTHORITATIVE"

    follow_request = _request(planner, followed.task.ref)
    facts = [item for item in follow_request.prior_outcomes if item.ref == outcome.ref]
    assert len(facts) == 1
    assert facts[0].subject_key == SUBJECT
    assert facts[0].statement == outcome.statement
    assert facts[0].status == "AUTHORITATIVE"
    assert all(item.outcome_ref != outcome.ref for item in follow_request.binding_constraints)
    assert _request(planner, analyzed.task.ref).prior_outcomes == ()
    assert followed.task.id != applied.task.id
    assert followed.decision is not None
    assert followed.decision.decision_type.value == "NO_ACTION"
    assert followed.actions == ()
