"""Agent runtime for the generic organization entry.

It carries out the node it is given and does not embed an industry scenario.
A decision proposes no action unless a later, configured runtime does.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.values import Confidence, EvidenceItem
from qor.kernel.clock import Clock
from qor.ports.agent_runtime import (
    DecisionProposal,
    DecisionRequest,
    DraftFinding,
    ReviewOpinion,
    ReviewRequest,
    WorkRequest,
    WorkResult,
)

__all__ = ["GenericAgentRuntime"]


class GenericAgentRuntime:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def execute_work(self, agent_id: UUID, request: WorkRequest) -> WorkResult:
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
            methodology="Record the claim and the artifacts it used.",
        )

    def execute_review(self, agent_id: UUID, request: ReviewRequest) -> ReviewOpinion:
        return ReviewOpinion(
            verdict="PASSED",
            summary=f"{request.artifact.ref} meets the review rubric.",
        )

    def propose_decision(self, agent_id: UUID, request: DecisionRequest) -> DecisionProposal:
        return DecisionProposal(
            decision_type="NO_ACTION",
            statement="No further organizational action is required.",
            rationale=f"The accepted account covers the request. Task context: {request.task_objective}",
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
