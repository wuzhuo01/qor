"""One generic organization task through the already proven QOR services.

The planner and agent runtime are ports. Plan, assignment, review, decision
and task transitions are the production services.
"""

from __future__ import annotations

from qor.app import build_app
from qor.domain.enums import ArtifactType
from qor.domain.task import NodeKind
from qor.domain.values import Confidence, EvidenceItem
from qor.ports.agent_runtime import (
    DecisionProposal,
    DecisionRequest,
    DraftFinding,
    ReviewOpinion,
    ReviewRequest,
    WorkRequest,
    WorkResult,
)
from qor.ports.planner import NodeDraft, PlanDraft, PlanRequest
from tests.generic_staff import ACCOUNT_RECORD, ACCOUNT_SELECT, ACCOUNT_SYNTHESIS, staffed_config

OBJECTIVE = (
    "Prepare the organizational status for this cycle and state what the next cycle should keep."
)
CAP_A = ACCOUNT_RECORD
CAP_B = ACCOUNT_SELECT
CAP_SYNTH = ACCOUNT_SYNTHESIS


class GenericOrgPlanner:
    kind = "test"
    provider = "generic-org"

    def __init__(self) -> None:
        self.calls = 0
        self.last_request: PlanRequest | None = None

    def plan(self, request: PlanRequest) -> PlanDraft:
        self.calls += 1
        self.last_request = request
        return PlanDraft(
            nodes=(
                NodeDraft(
                    key="status",
                    name="Assemble the cycle status",
                    objective="Assemble the facts that belong in this cycle's status.",
                    required_capabilities=(CAP_A,),
                    expected_artifact_type=ArtifactType.ANALYSIS_REPORT,
                ),
                NodeDraft(
                    key="keep",
                    name="Select what the next cycle should keep",
                    objective="Use the accepted status to select what the next cycle should keep.",
                    required_capabilities=(CAP_B,),
                    expected_artifact_type=ArtifactType.ANALYSIS_REPORT,
                    depends_on=("status",),
                ),
                NodeDraft(
                    key="synth",
                    name="Integrate the cycle account",
                    objective="Integrate the status and the keep-list into one account.",
                    required_capabilities=(CAP_SYNTH,),
                    expected_artifact_type=ArtifactType.SYNTHESIS,
                    node_kind=NodeKind.SYNTHESIS,
                    depends_on=("keep",),
                ),
                NodeDraft(
                    key="recommend",
                    name="State the organizational recommendation",
                    objective="Turn the integrated account into the cycle recommendation.",
                    required_capabilities=(CAP_SYNTH,),
                    expected_artifact_type=ArtifactType.RECOMMENDATION,
                    node_kind=NodeKind.SYNTHESIS,
                    depends_on=("synth",),
                ),
            ),
            strategy_rationale=(
                "Two capability-addressed work nodes, the second depending on the first, "
                "then synthesis and a recommendation. No agent is named."
            ),
        )


class GenericOrgAgents:
    def __init__(self, clock) -> None:
        self._clock = clock
        self.work_requests: list[WorkRequest] = []
        self.review_requests: list[ReviewRequest] = []
        self.decision_requests: list[DecisionRequest] = []

    def execute_work(self, agent_id, request: WorkRequest) -> WorkResult:
        self.work_requests.append(request)
        used = ", ".join(artifact.ref for artifact in request.input_artifacts)
        locator = f"org://{request.node_ref}/attempt-{request.attempt}"
        statement = f"{request.node_name} completed."
        if used:
            statement = f"{statement} Consumed {used}."
        return WorkResult(
            title=request.node_name,
            content=statement,
            findings=(DraftFinding(statement=statement, evidence_locators=(locator,)),),
            evidence=(
                EvidenceItem(
                    kind="record",
                    summary=statement,
                    locator=locator,
                    observed_at=self._clock.now(),
                ),
            ),
            confidence=Confidence(value=0.8, basis="the node objective and its accepted inputs"),
            methodology="Record the organizational claim and the artifacts it used.",
        )

    def execute_review(self, agent_id, request: ReviewRequest) -> ReviewOpinion:
        self.review_requests.append(request)
        return ReviewOpinion(
            verdict="PASSED",
            summary=f"{request.artifact.ref} meets the review rubric.",
        )

    def propose_decision(self, agent_id, request: DecisionRequest) -> DecisionProposal:
        self.decision_requests.append(request)
        return DecisionProposal(
            decision_type="NO_ACTION",
            statement="No further organizational action is required for this cycle.",
            rationale=(
                "The accepted account already states the status and what to keep. "
                f"Task context: {request.task_objective}"
            ),
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


def test_generic_organization_loop_composes_proven_capabilities(clock):
    planner = GenericOrgPlanner()
    agents = GenericOrgAgents(clock)
    app = build_app(planner=planner, agents=agents, clock=clock, config=staffed_config())
    result = app.orchestrator.run_objective(
        OBJECTIVE,
        required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
    )

    assert planner.calls == 1
    assert planner.last_request is not None
    assert "agent_id" not in planner.last_request.model_dump_json()
    assert len(agents.work_requests) >= 4
    assert agents.review_requests
    assert len(agents.decision_requests) == 1

    task = result.task
    assert task.status == "COMPLETED"
    assert task.active_plan_id is not None
    plan = app.runtime.repos.plans.get(task.active_plan_id)
    assert plan.status == "ACTIVE"
    nodes = [app.runtime.repos.nodes.get(node_id) for node_id in plan.node_ids]
    status = next(node for node in nodes if node.required_capabilities == (CAP_A,))
    keep = next(node for node in nodes if node.required_capabilities == (CAP_B,))
    assert status.status == "ACCEPTED"
    assert keep.status == "ACCEPTED"
    assert status.id in plan.upstream_of(keep.id)

    assignments = [item for item in app.runtime.repos.assignments.all() if item.task_id == task.id]
    status_assignee = next(
        item.assignee_agent_id for item in assignments if item.node_id == status.id
    )
    keep_assignee = next(item.assignee_agent_id for item in assignments if item.node_id == keep.id)
    assert status_assignee != keep_assignee

    status_artifact = app.runtime.repos.artifacts.get(status.output_artifact_ids[-1])
    keep_request = next(
        item for item in agents.work_requests if item.required_capabilities == (CAP_B,)
    )
    assert status_artifact.ref in {item.ref for item in keep_request.input_artifacts}
    assert status_artifact.status == "ACCEPTED"

    reviews = [item for item in app.runtime.repos.reviews.all() if item.task_id == task.id]
    assert reviews
    assert all(item.status == "PASSED" for item in reviews)
    artifact_ids = {
        item.id for item in app.runtime.repos.artifacts.all() if item.task_id == task.id
    }
    assert all(item.artifact_id in artifact_ids for item in reviews)

    decision = result.decision
    assert decision is not None
    assert decision.status == "DECIDED"
    assert decision.decision_type.value == "NO_ACTION"
    assert set(decision.based_on_artifact_ids) <= artifact_ids
    assert decision.based_on_review_ids
    assert result.actions == ()
    assert not any(item.status == "AUTHORITATIVE" for item in app.runtime.repos.outcomes.all())

    transitions = [
        item.transition
        for item in app.runtime.log.transitions_for(task.id)
        if item.entity_type == "Task"
    ]
    for name in (
        "admit",
        "start_planning",
        "plan_approved",
        "start",
        "nodes_submitted",
        "reviews_passed",
        "synthesis_ready",
        "decided_no_action",
    ):
        assert name in transitions
