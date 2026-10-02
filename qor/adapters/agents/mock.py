"""Mock agent runtime.

Dispatch is by capability and expected artifact type, never by agent name.
``execute_work`` uses ``agent_id`` only for bookkeeping. An unknown capability
falls through to a generic producer rather than crashing.

The reviewer applies the policy rubric to the artifact. ``weaken_first_attempt_for``
makes a producer omit its support on the first attempt; the reviewer then does
its actual job.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from qor.domain.decision import SuccessCheck
from qor.domain.enums import ArtifactType, FindingSeverity
from qor.domain.values import Confidence, EvidenceItem, Uncertainty
from qor.kernel.clock import Clock
from qor.ports.agent_runtime import (
    ActionProposal,
    DecisionProposal,
    DecisionRequest,
    DraftFinding,
    OpinionFinding,
    ReviewOpinion,
    ReviewRequest,
    WorkRequest,
    WorkResult,
)

__all__ = ["MockAgentRuntime"]

MEASURED_ARTIFACTS = (ArtifactType.ANALYSIS_RESULT, ArtifactType.VALIDATION_REPORT)


class MockAgentRuntime:
    def __init__(
        self,
        clock: Clock,
        *,
        weaken_first_attempt_for: frozenset[str] = frozenset(),
        decision_addresses_outcome_ref: str | None = None,
        world_effect: bool = False,
        measurement_success_checks: tuple[SuccessCheck, ...] = (),
        measurement_evaluate_when: str = "",
        measurement_extra_metrics: tuple[str, ...] = (),
    ) -> None:
        self._clock = clock
        self._weaken = weaken_first_attempt_for
        self._decision_addresses_outcome_ref = decision_addresses_outcome_ref
        self._world_effect = world_effect
        self._measurement_success_checks = measurement_success_checks
        self._measurement_evaluate_when = measurement_evaluate_when
        self._measurement_extra_metrics = measurement_extra_metrics
        self.work_calls: list[tuple[UUID, str]] = []
        self.review_calls: list[tuple[UUID, str]] = []

    def execute_work(self, agent_id: UUID, request: WorkRequest) -> WorkResult:
        capability = request.required_capabilities[0]
        self.work_calls.append((agent_id, capability))
        result = _produce(self, request)
        if capability in self._weaken and request.attempt == 1:
            return result.model_copy(
                update={
                    "methodology": "",
                    "uncertainty": Uncertainty(),
                    "confidence": None,
                }
            )
        return result

    def execute_review(self, agent_id: UUID, request: ReviewRequest) -> ReviewOpinion:
        """Apply the rubric mechanically. No randomness."""
        self.review_calls.append((agent_id, request.artifact.ref))
        artifact = request.artifact
        findings: list[OpinionFinding] = []
        required: list[str] = []

        if not artifact.findings:
            findings.append(
                OpinionFinding(
                    severity=FindingSeverity.BLOCKER,
                    statement="R1 violated: artifact states no findings",
                    suggested_fix="State at least one finding with supporting evidence",
                )
            )
            required.append("state at least one finding backed by evidence")

        if not artifact.methodology.strip():
            findings.append(
                OpinionFinding(
                    severity=FindingSeverity.BLOCKER,
                    statement=(
                        "R2 violated: methodology is not stated, so the result cannot be "
                        "reproduced or challenged"
                    ),
                    location="methodology",
                    suggested_fix="Describe the record window, transformation and check used",
                )
            )
            required.append("state the methodology: record window, transformation and check used")

        declared = (
            artifact.uncertainty.assumptions
            + artifact.uncertainty.known_unknowns
            + artifact.uncertainty.data_gaps
        )
        if not declared:
            findings.append(
                OpinionFinding(
                    severity=FindingSeverity.BLOCKER,
                    statement=(
                        "R3 violated: no assumptions or known unknowns declared, so the "
                        "conclusion's fragility is unassessable"
                    ),
                    location="uncertainty",
                    suggested_fix="Declare the assumptions the conclusion depends on",
                )
            )
            required.append("declare the assumptions and known unknowns behind the conclusion")

        if artifact.confidence is None:
            findings.append(
                OpinionFinding(
                    severity=FindingSeverity.MAJOR,
                    statement="R4 violated: no confidence with a stated basis",
                    location="confidence",
                    suggested_fix="State a confidence value and what it is based on",
                )
            )
            required.append("state a confidence value together with its basis")

        if artifact.artifact_type in MEASURED_ARTIFACTS and (
            artifact.confidence is None or artifact.confidence.sample_size is None
        ):
            findings.append(
                OpinionFinding(
                    severity=FindingSeverity.BLOCKER,
                    statement=(
                        "R5 violated: a measured claim is made without reporting a sample size"
                    ),
                    location="confidence.sample_size",
                    suggested_fix="Report the number of observations the claim rests on",
                )
            )
            required.append("report the sample size supporting the measured claim")

        if findings:
            worst = max(f.severity for f in findings)
            verdict = "FAILED" if worst >= FindingSeverity.MAJOR else "PASSED"
        else:
            verdict = "PASSED"

        summary = f"round {request.round} rubric review of {artifact.ref}: " + (
            f"{len(findings)} issue(s) found -- "
            + "; ".join(f.statement.split(":")[0] for f in findings)
            if findings
            else f"all {len(request.rubric)} rubric items satisfied"
        )
        if request.round > 1 and request.previous_required_changes and not findings:
            summary += (
                ". Previous round's required changes were addressed: "
                f"{list(request.previous_required_changes)}"
            )

        return ReviewOpinion(
            verdict=verdict,
            summary=summary,
            findings=tuple(findings),
            required_changes=tuple(required),
        )

    def propose_decision(self, agent_id: UUID, request: DecisionRequest) -> DecisionProposal:
        """Recommend pausing the current workflow.

        The proposed action stays reversible. ``permitted_environments`` is honoured.
        """
        environment = request.permitted_environments[0]
        evidence_refs = [a.ref for a in request.accepted_artifacts]
        synthesis = next(
            (a for a in request.accepted_artifacts if a.artifact_type is ArtifactType.SYNTHESIS),
            None,
        )
        basis = (
            synthesis.findings[0].statement
            if synthesis and synthesis.findings
            else "the accepted evidence set"
        )
        addressed = (
            request.addresses_outcome_ref
            if self._decision_addresses_outcome_ref is None
            else self._decision_addresses_outcome_ref
        )
        proposed_actions = (
            (
                ActionProposal(
                    system="sandbox-world",
                    resource="ledger",
                    environment=environment,
                    operation="increment",
                    capability_key="sandbox.world_increment",
                    payload={"delta": 1},
                    is_reversible=True,
                    dry_run=False,
                ),
            )
            if self._world_effect
            else (
                ActionProposal(
                    system="organization",
                    resource="workflow",
                    environment=environment,
                    operation="pause",
                    capability_key="governance.decision",
                    payload={"reason": "unresolved evidence pending review"},
                    is_reversible=True,
                ),
            )
        )
        return DecisionProposal(
            decision_type="WORKFLOW_PAUSE",
            statement=(
                "Pause the current workflow pending review, rather than continuing while "
                f"the recorded evidence is unresolved. Task context: {request.task_objective}"
            ),
            rationale=(
                f"The accepted evidence ({', '.join(evidence_refs)}) supports a pause: {basis} "
                f"Task context: {request.task_objective} "
                "Pausing is reversible. Continuing would mix a new change into an "
                "unresolved record."
            ),
            alternatives=(
                {
                    "option": "Continue the workflow unchanged",
                    "pros": ["No intervention", "Preserves the current record"],
                    "cons": ["Leaves the unresolved record in place", "Learns nothing new"],
                    "rejected_because": "the recorded gap is already outside the stated check",
                },
                {
                    "option": "Change two parts of the workflow at once",
                    "pros": ["Might close the gap faster"],
                    "cons": ["Mixes two changes", "Removes a basis for explaining the result later"],
                    "rejected_because": (
                        "afterwards it would be unclear which change produced the result"
                    ),
                },
                {
                    "option": "Pause pending review",
                    "pros": ["Reversible", "Keeps the current record intact"],
                    "cons": ["Delays the next step"],
                    "rejected_because": "",
                },
            ),
            metrics=_measurement_metrics(request.task_objective, self),
            evaluate_after="30 days, or immediately when the recorded condition changes",
            success_criteria=(
                "the named workflow check is met after review",
                "the unresolved count does not increase past the level at pause time",
            ),
            success_checks=self._measurement_success_checks,
            evaluate_when=self._measurement_evaluate_when,
            confidence=Confidence(
                value=0.72,
                basis=(
                    "the accepted artifacts agree on the recorded gap; "
                    "the measured sample is short"
                ),
                sample_size=len(request.accepted_artifacts),
            ),
            risk_level=request.risk_level,
            is_reversible=True,
            proposed_actions=proposed_actions,
            addresses_outcome_ref=addressed,
        )

    def _now(self) -> datetime:
        return self._clock.now()


def _measurement_metrics(objective: str, runtime: MockAgentRuntime) -> tuple[str, ...]:
    base = (
        "workflow.open_questions",
        "workflow.evidence_count",
        "workflow.unresolved_items",
    )
    if not objective.strip():
        base = ("workflow.open_questions",)
    extra = list(runtime._measurement_extra_metrics)
    for check in runtime._measurement_success_checks:
        metric = getattr(check, "metric", "")
        if metric and metric not in base and metric not in extra:
            extra.append(metric)
    return (*base, *extra)


def _evidence(kind: str, summary: str, locator: str, at: datetime, by: str) -> EvidenceItem:
    return EvidenceItem(kind=kind, summary=summary, locator=locator, observed_at=at, produced_by=by)


def _produce(rt: MockAgentRuntime, request: WorkRequest) -> WorkResult:
    expected = request.expected_artifact_type
    if expected is ArtifactType.VALIDATION_REPORT:
        return _validation(rt, request)
    if expected is ArtifactType.ANALYSIS_RESULT:
        return _metric(rt, request)
    if expected in (ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION):
        return _synthesis(rt, request)
    return _observation(rt, request)


def _observation(rt: MockAgentRuntime, request: WorkRequest) -> WorkResult:
    at = rt._now()
    capability = request.required_capabilities[0]
    ev = (
        _evidence(
            "observation",
            f"Recorded observation for {request.node_ref}",
            f"record://{request.node_ref}/observation",
            at,
            capability,
        ),
    )
    return WorkResult(
        title=f"Observation: {request.node_name}",
        content=(
            f"Recorded an observation for: {request.node_objective}. "
            "The record states what was seen. It does not assert a cause."
        ),
        findings=(
            DraftFinding(
                statement=f"{request.node_name} recorded an observation",
                evidence_locators=(ev[0].locator,),
                confidence=Confidence(
                    value=0.7,
                    basis="one recorded observation",
                    sample_size=1,
                ),
            ),
        ),
        evidence=ev,
        confidence=Confidence(value=0.7, basis="one recorded observation", sample_size=1),
        uncertainty=Uncertainty(
            assumptions=("the record window matches the request",),
            known_unknowns=("whether a later record will agree",),
        ),
        methodology="Read the named record and wrote one observation.",
        data_sources=(f"record://{request.node_ref}",),
        cost_units=1,
    )


def _metric(rt: MockAgentRuntime, request: WorkRequest) -> WorkResult:
    at = rt._now()
    capability = request.required_capabilities[0]
    ev = (
        _evidence(
            "metric",
            f"Counted items for {request.node_ref}",
            f"metric://{request.node_ref}/count",
            at,
            capability,
        ),
    )
    return WorkResult(
        title=f"Metric: {request.node_name}",
        content=(
            f"Counted the items named by: {request.node_objective}. "
            "The count is the recorded measurement."
        ),
        findings=(
            DraftFinding(
                statement=f"{request.node_name} recorded a count",
                evidence_locators=(ev[0].locator,),
                confidence=Confidence(
                    value=0.8,
                    basis="counted from the named record",
                    sample_size=1,
                ),
            ),
        ),
        evidence=ev,
        confidence=Confidence(value=0.8, basis="counted from the named record", sample_size=1),
        uncertainty=Uncertainty(
            assumptions=("each item was counted once",),
            known_unknowns=("items outside the record window",),
        ),
        methodology="Counted items in the named record.",
        data_sources=(f"metric://{request.node_ref}",),
        cost_units=1,
    )


def _validation(rt: MockAgentRuntime, request: WorkRequest) -> WorkResult:
    at = rt._now()
    capability = request.required_capabilities[0]
    ev = (
        _evidence(
            "record",
            f"Check result for {request.node_ref}",
            f"record://{request.node_ref}/check",
            at,
            capability,
        ),
    )
    return WorkResult(
        title=f"Validation: {request.node_name}",
        content=(
            f"Checked the record for: {request.node_objective}. "
            "The check compares the record with the stated expectation."
        ),
        findings=(
            DraftFinding(
                statement=f"{request.node_name} completed a record check",
                evidence_locators=(ev[0].locator,),
                confidence=Confidence(
                    value=0.75,
                    basis="one check against the named record",
                    sample_size=1,
                ),
            ),
        ),
        evidence=ev,
        confidence=Confidence(value=0.75, basis="one check against the named record", sample_size=1),
        uncertainty=Uncertainty(
            assumptions=("the expectation was stated before the check",),
            known_unknowns=("records not included in this check",),
        ),
        methodology="Compared the named record with the stated expectation.",
        data_sources=(f"record://{request.node_ref}",),
        cost_units=1,
    )


def _synthesis(rt: MockAgentRuntime, request: WorkRequest) -> WorkResult:
    at = rt._now()
    inputs = request.input_artifacts
    upstream = [f.statement for a in inputs for f in a.findings]
    ev = tuple(
        _evidence(
            "artifact",
            f"{a.artifact_type.value} '{a.title}' with {len(a.findings)} finding(s)",
            f"artifact://{a.ref}#{a.content_hash[:12]}",
            at,
            a.ref,
        )
        for a in inputs
    ) or (
        _evidence(
            "artifact",
            "no upstream artifacts were available",
            "artifact://none",
            at,
            request.node_ref,
        ),
    )
    if request.expected_artifact_type is ArtifactType.RECOMMENDATION:
        return WorkResult(
            title=f"Recommendation: {request.node_name}",
            content=(
                "The accepted records agree on one next step: pause the workflow "
                "pending review. Pausing is reversible."
            ),
            findings=(
                DraftFinding(
                    statement="Recommend pausing the workflow pending review",
                    evidence_locators=tuple(e.locator for e in ev),
                    confidence=Confidence(
                        value=0.72,
                        basis=f"{len(inputs)} accepted artifacts",
                        sample_size=len(inputs),
                    ),
                ),
            ),
            evidence=ev,
            confidence=Confidence(
                value=0.72,
                basis=f"{len(inputs)} accepted artifacts",
                sample_size=len(inputs),
            ),
            uncertainty=Uncertainty(
                assumptions=("the accepted artifacts describe the same workflow",),
                known_unknowns=("how long the review will take",),
            ),
            methodology="Weighed continue, change, and pause against the accepted records.",
            cost_units=1,
        )
    joined = "\n".join(f"  - {s}" for s in upstream)
    return WorkResult(
        title=f"Synthesis: {request.node_name}",
        content=(
            "The accepted records were compared for agreement. "
            "No record contradicts the others.\n\n"
            f"Upstream findings integrated:\n{joined}"
        ),
        findings=(
            DraftFinding(
                statement="The accepted records agree, and none contradicts the others",
                evidence_locators=tuple(e.locator for e in ev),
                confidence=Confidence(
                    value=0.8,
                    basis=f"{len(inputs)} accepted artifacts",
                    sample_size=len(inputs),
                ),
            ),
        ),
        evidence=ev,
        confidence=Confidence(
            value=0.8,
            basis=f"{len(inputs)} accepted artifacts",
            sample_size=len(inputs),
        ),
        uncertainty=Uncertainty(
            assumptions=("the upstream artifacts were produced independently",),
            known_unknowns=("records not included in this set",),
        ),
        methodology="Cross-checked upstream findings for agreement and contradiction.",
        cost_units=1,
    )
