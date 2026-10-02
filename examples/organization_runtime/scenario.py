"""Scripted organization runtime example.

The organization evaluates whether an internal change may enter the next stage.
Agents are scripts. Planning is StubPlanner. No model is called.

``full`` walks Goal, Plan, Assignment, Artifact, Evidence, Review, Decision,
and Outcome. ``gap`` stops when a planned capability has no holder.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from qor.adapters.planner.stub import StubPlanner
from qor.app import QORApp, build_app
from qor.domain.enums import ArtifactType, ClaimClass, RiskLevel
from qor.domain.outcome import Outcome
from qor.domain.values import Confidence, EvidenceItem, Uncertainty
from qor.kernel.clock import FrozenClock
from qor.ports.agent_runtime import (
    DecisionProposal,
    DecisionRequest,
    DraftFinding,
    ReviewOpinion,
    ReviewRequest,
    WorkRequest,
    WorkResult,
)
from qor.services.config import QORConfig
from qor.services.orchestrator import RunResult

OBJECTIVE = "评估一个内部变化是否可以进入下一阶段。"

CAP_EXTERNAL = "organization.external_record"
CAP_TOOL = "organization.tool_record"
CAP_REASONING = "organization.reasoning_note"
CAP_STAGE = "organization.stage_recommendation"
CAP_MISSING = "organization.unassigned_check"

_WORK = (
    {
        "key": CAP_EXTERNAL,
        "name": "Record an external change package",
        "description": "Record a change package supplied from outside the producing agent.",
        "risk_class": "READ_ONLY",
    },
    {
        "key": CAP_TOOL,
        "name": "Record a tool checklist",
        "description": "Record the result of a checklist tool.",
        "risk_class": "READ_ONLY",
    },
    {
        "key": CAP_REASONING,
        "name": "Note a readiness hypothesis",
        "description": "Record the producing agent's own reasoning about readiness.",
        "risk_class": "ADVISORY",
    },
    {
        "key": CAP_STAGE,
        "name": "Recommend a stage gate",
        "description": "Integrate accepted records into a stage recommendation.",
        "risk_class": "ADVISORY",
    },
)

_MISSING = {
    "key": CAP_MISSING,
    "name": "Unassigned check",
    "description": "A plannable check that this organization does not staff.",
    "risk_class": "READ_ONLY",
}


def organization_config(*, missing_capability: bool) -> QORConfig:
    """Generic organization plus three agents and plannable work capabilities."""
    base = QORConfig.load()
    organization = copy.deepcopy(base.organization)
    capabilities = copy.deepcopy(base.capabilities)
    roles = copy.deepcopy(base.roles)

    extra = [_MISSING, *_WORK] if missing_capability else list(_WORK)
    capabilities["capabilities"] = [*capabilities.get("capabilities", ()), *extra]

    for role in roles["roles"]:
        if role["key"] == "role.human_cio":
            role["name"] = "CIO"
            role["granted_permissions"] = [
                *role.get("granted_permissions", ()),
                "plan.create",
                "decision.propose",
                "assignment.create",
            ]
    roles["roles"] = [
        *roles["roles"],
        {
            "key": "role.analyst",
            "name": "Analyst",
            "purpose": "Produce assigned artifacts. Does not review them or decide.",
            "max_risk_level": "ELEVATED",
            "granted_permissions": ["artifact.create", "artifact.submit"],
        },
    ]

    held = [{"capability_key": spec["key"], "proficiency": 0.8} for spec in _WORK]
    organization["organization"]["name"] = "Organization"
    organization["organization"]["mission"] = (
        "Admit work, assign it by capability, and decide only from accepted evidence."
    )
    organization["departments"] = [
        {
            "key": "dept.governance",
            "name": "Governance",
            "mandate": "Admit work, review it, and decide.",
            "owns_capabilities": [
                "governance.review",
                "governance.planning",
                "governance.decision",
                *(spec["key"] for spec in extra),
            ],
        }
    ]
    organization["agents"] = [
        {
            "key": "agent.cio",
            "name": "CIO",
            "kind": "HUMAN",
            "roles": ["role.human_cio"],
            "department": "dept.governance",
            "max_concurrent_assignments": 4,
            "trust_score": 1.0,
            "capabilities": [],
        },
        {
            "key": "agent.analyst",
            "name": "Analyst",
            "kind": "AGENT",
            "roles": ["role.analyst"],
            "department": "dept.governance",
            "max_concurrent_assignments": 4,
            "trust_score": 0.8,
            "capabilities": held,
        },
        {
            "key": "agent.reviewer",
            "name": "Reviewer",
            "kind": "AGENT",
            "roles": ["role.reviewer"],
            "department": "dept.governance",
            "max_concurrent_assignments": 4,
            "trust_score": 0.8,
            "capabilities": [{"capability_key": "governance.review", "proficiency": 0.8}],
        },
    ]
    organization["human_cio"] = "agent.cio"
    organization["default_task_owner"] = "agent.cio"
    return QORConfig(
        organization=organization,
        capabilities=capabilities,
        roles=roles,
        permissions=base.permissions,
        policies=base.policies,
        state_machine_dir=base.state_machine_dir,
    )


class ScriptedOrganizationAgents:
    """Analyst, Reviewer, and CIO as fixed scripts. No model client."""

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock

    def execute_work(self, agent_id: UUID, request: WorkRequest) -> WorkResult:
        key = request.required_capabilities[0]
        if key == CAP_EXTERNAL:
            return self._result(
                agent_id,
                request,
                kind="external",
                claim_class=ClaimClass.FACT,
                statement="An external change package is on record for this stage gate.",
                locator="record://internal-change/package",
            )
        if key == CAP_TOOL:
            return self._result(
                agent_id,
                request,
                kind="tool",
                claim_class=ClaimClass.FACT,
                statement="The checklist tool reports the change package as complete.",
                locator="tool://change-checklist/run-1",
            )
        if key == CAP_REASONING and request.attempt == 1:
            return self._result(
                agent_id,
                request,
                kind="reasoning",
                claim_class=ClaimClass.FACT,
                statement="The change is ready for the next stage.",
                locator=f"reasoning://stage-readiness/attempt-{request.attempt}",
            )
        if key == CAP_REASONING:
            return self._result(
                agent_id,
                request,
                kind="reasoning",
                claim_class=ClaimClass.HYPOTHESIS,
                statement="The change may be ready for the next stage.",
                locator=f"reasoning://stage-readiness/attempt-{request.attempt}",
            )
        return self._result(
            agent_id,
            request,
            kind="reasoning",
            claim_class=ClaimClass.DIRECTION,
            statement=(
                "Recommend the next stage only from accepted external and tool records. "
                "Treat readiness reasoning as a hypothesis."
            ),
            locator=f"reasoning://stage-recommendation/{request.node_ref}",
        )

    def execute_review(self, agent_id: UUID, request: ReviewRequest) -> ReviewOpinion:
        del agent_id, request
        return ReviewOpinion(verdict="PASSED", summary="Reviewer accepts the submitted wording.")

    def propose_decision(self, agent_id: UUID, request: DecisionRequest) -> DecisionProposal:
        del agent_id
        basis = []
        for artifact in request.accepted_artifacts:
            kinds = ",".join(sorted({item.kind for item in artifact.evidence}))
            classes = ",".join(
                sorted(
                    {
                        finding.claim_class.value
                        for finding in artifact.findings
                        if finding.claim_class is not None
                    }
                )
            )
            basis.append(f"{artifact.ref} evidence={kinds or 'none'} claims={classes or 'none'}")
        return DecisionProposal(
            decision_type="RECOMMEND",
            statement=(
                "The internal change may enter the next stage on accepted external and tool "
                "records. Readiness asserted only as reasoning is not a fact."
            ),
            rationale="Decision basis: " + "; ".join(basis),
            alternatives=(
                {
                    "option": "Enter the next stage because the reasoning note says it is ready",
                    "rejected_because": (
                        "a factual readiness claim cited only reasoning and review rejected it"
                    ),
                },
            ),
            metrics=("stage_gate_recorded",),
            evaluate_after="the next stage records whether the change was admitted",
            success_criteria=("the next stage records an admission decision",),
            risk_level=request.risk_level,
            proposed_actions=(),
        )

    def _result(
        self,
        agent_id: UUID,
        request: WorkRequest,
        *,
        kind: str,
        claim_class: ClaimClass,
        statement: str,
        locator: str,
    ) -> WorkResult:
        return WorkResult(
            title=request.node_name,
            content=statement,
            findings=(
                DraftFinding(
                    statement=statement,
                    evidence_locators=(locator,),
                    claim_class=claim_class,
                ),
            ),
            evidence=(
                EvidenceItem(
                    kind=kind,
                    summary=statement,
                    locator=locator,
                    observed_at=self._clock.now(),
                    produced_by=str(agent_id),
                ),
            ),
            confidence=Confidence(value=0.6, basis=f"{kind} evidence on {request.node_ref}"),
            uncertainty=Uncertainty(
                assumptions=("the change package is the one named by the objective",),
                known_unknowns=("whether the next stage will admit the change",),
            ),
            methodology="Scripted agent files one claim and one evidence item.",
        )


@dataclass(frozen=True)
class ExampleRun:
    name: str
    app: QORApp
    result: RunResult
    outcome: Outcome | None


def run_full() -> ExampleRun:
    app = _app(missing_capability=False)
    task = app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        title="Internal change stage gate",
        requested_by_agent_id=app.runtime.agent_index["agent.cio"],
        risk_level=RiskLevel.ELEVATED,
        required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
    )
    result = app.orchestrator.run_task(task)
    outcome = None
    if result.decision is not None and result.decision.status == "DECIDED":
        outcome = app.orchestrator.outcomes.record(
            decision_id=result.decision.id,
            subject_key="internal_change.stage_gate",
            verdict="UNCERTAIN",
            contradicted_decision_type=result.decision.decision_type.value,
            confidence="LOW",
            statement=(
                "The recommendation is recorded. Whether the next stage admits the "
                "change is not yet observed."
            ),
            recorded_by_agent_id=app.runtime.agent_index["agent.cio"],
            measurement_metrics=result.decision.measurement_plan.metrics[:1],
        )
    return ExampleRun("full", app, result, outcome)


def run_gap() -> ExampleRun:
    app = _app(missing_capability=True)
    task = app.orchestrator.intake.submit(
        objective=OBJECTIVE,
        title="Internal change stage gate",
        requested_by_agent_id=app.runtime.agent_index["agent.cio"],
        risk_level=RiskLevel.ELEVATED,
        required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
    )
    result = app.orchestrator.run_task(task)
    return ExampleRun("gap", app, result, None)


def render(run: ExampleRun) -> str:
    app = run.app
    task = app.runtime.repos.tasks.get(run.result.task.id)
    artifacts = [
        item
        for item in sorted(app.runtime.repos.artifacts.all(), key=lambda item: item.ref)
        if item.task_id == task.id
    ]
    reviews = [
        item
        for item in sorted(app.runtime.repos.reviews.all(), key=lambda item: item.ref)
        if item.task_id == task.id
    ]
    producers = sorted(
        {
            _agent_name(app, item.produced_by)
            for artifact in artifacts
            for item in artifact.evidence
            if item.produced_by
        }
    )
    reviewers = sorted(
        {
            app.runtime.repos.agents.get(item.reviewer_agent_id).name
            for item in reviews
            if item.reviewer_agent_id is not None
        }
    )
    evidence = sorted({item.kind for artifact in artifacts for item in artifact.evidence})
    decision = run.result.decision
    blocked = [
        step.reason_code
        for step in app.runtime.log.transitions_for(task.id)
        if step.transition == "block"
    ]
    if run.name == "gap":
        chain = ["Capability Gap", " ↓", "BLOCKED"]
    else:
        chain = [
            "Goal",
            " ↓",
            "Plan",
            " ↓",
            "Assignment",
            " ↓",
            "Artifact",
            " ↓",
            "Review",
            " ↓",
            "Decision",
            " ↓",
            "Outcome",
        ]
    lines = [
        f"scenario: {run.name}",
        *chain,
        "",
        "This example uses:",
        "- StubPlanner",
        "- scripted agents",
        "- no LLM",
        "",
        f"producer: {', '.join(producers) if producers else '(none)'}",
        f"reviewer: {', '.join(reviewers) if reviewers else '(none)'}",
        f"evidence: {', '.join(evidence) if evidence else '(none)'}",
        (
            f"decision: {decision.ref} {decision.decision_type.value}"
            if decision is not None
            else "decision: (none)"
        ),
        (
            f"reason: {decision.rationale}"
            if decision is not None
            else f"reason: {blocked[-1] if blocked else task.status}"
        ),
        "",
        "organization",
        f"  name: {app.runtime.organization().name}",
        f"  mission: {app.runtime.organization().mission}",
    ]
    for agent in sorted(app.runtime.repos.agents.all(), key=lambda item: item.name):
        keys = ", ".join(cap.capability_key for cap in agent.capabilities) or "(none)"
        lines.append(f"  agent: {agent.name} kind={agent.kind.value} capabilities={keys}")

    lines.extend(["", "task timeline"])
    for step in app.runtime.log.transitions_for(task.id):
        lines.append(
            f"  {step.entity_ref} {step.from_state or '<new>'} -> {step.to_state} "
            f"{step.transition} {step.reason_code}"
        )

    lines.extend(["", "artifacts"])
    if not artifacts:
        lines.append("  (none)")
    for artifact in artifacts:
        lines.append(f"  {artifact.ref} status={artifact.status} title={artifact.title}")

    lines.extend(["", "evidence sources"])
    if not artifacts:
        lines.append("  (none)")
    for artifact in artifacts:
        for item in artifact.evidence:
            lines.append(
                f"  {artifact.ref} kind={item.kind} locator={item.locator} "
                f"producer={_agent_name(app, item.produced_by)}"
            )

    lines.extend(["", "review result"])
    if not reviews:
        lines.append("  (none)")
    for review in reviews:
        artifact = app.runtime.repos.artifacts.get(review.artifact_id)
        kinds = ",".join(sorted({item.kind for item in artifact.evidence}))
        reviewer = app.runtime.repos.agents.get(review.reviewer_agent_id).name
        lines.append(
            f"  {review.ref} round={review.round} verdict={review.verdict} "
            f"reviewer={reviewer} artifact={artifact.ref} evidence={kinds}"
        )

    lines.extend(["", "decision"])
    if decision is None:
        lines.append("  (none)")
    else:
        lines.append(
            f"  {decision.ref} type={decision.decision_type.value} status={decision.status}"
        )
        lines.append(f"  statement: {decision.statement}")
        lines.append(f"  rationale: {decision.rationale}")
        cited = ", ".join(
            app.runtime.repos.artifacts.get(item_id).ref
            for item_id in decision.based_on_artifact_ids
        )
        lines.append(f"  based_on: {cited}")

    lines.extend(["", "outcome"])
    if run.outcome is None:
        lines.append("  (none)")
    else:
        lines.append(
            f"  {run.outcome.ref} verdict={run.outcome.verdict} "
            f"confidence={run.outcome.confidence} subject={run.outcome.subject_key}"
        )
        lines.append(f"  statement: {run.outcome.statement}")

    lines.extend(
        [
            "",
            "audit verification",
            f"  audit chain valid: {app.runtime.log.verify_audit_chain()}",
            f"  task status: {task.status}",
        ]
    )
    if blocked:
        lines.append(f"  block reason: {blocked[-1]}")
    return "\n".join(lines)


def _agent_name(app: QORApp, produced_by: str) -> str:
    for agent in app.runtime.repos.agents.all():
        if produced_by in {str(agent.id), agent.ref, agent.name}:
            return agent.name
    return produced_by or "(none)"


def _app(*, missing_capability: bool) -> QORApp:
    clock = FrozenClock(datetime(2026, 10, 2, tzinfo=UTC))
    return build_app(
        planner=StubPlanner(),
        agents=ScriptedOrganizationAgents(clock),
        clock=clock,
        config=organization_config(missing_capability=missing_capability),
    )
