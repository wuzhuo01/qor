"""Review -- the only quality exit (invariant I5).

The reviewer is chosen the same way any other worker is: by capability, through
delegation, with the producer excluded. Nothing here knows a reviewer's name.

Two design choices worth stating:

* **QOR derives the verdict from the findings**, then checks the reviewer's proposed
  verdict against it. A reviewer that reports a BLOCKER while voting PASSED is not
  quietly corrected -- the review is blocked and the node escalates, because a
  reviewer contradicting its own evidence is a governance failure, not a rounding
  error.
* **Rework never reopens a review.** Round n stays FAILED forever; round n+1 is a
  new object. That is ruling C4/E2, and it is the only way "why did round 1 fail"
  survives.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.artifact import Artifact
from qor.domain.enums import FindingSeverity, ReviewType
from qor.domain.organization import Agent
from qor.domain.review import Review, ReviewCondition, ReviewFinding
from qor.domain.task import Task, TaskNode
from qor.kernel.actor import ActorRef
from qor.kernel.errors import QORError
from qor.ports.agent_runtime import AgentRuntimePort, ReviewOpinion, ReviewRequest
from qor.services.delegation import DelegationService, Selection
from qor.services.evidence_source import apply_evidence_source_rule
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["ReviewOutcome", "ReviewService"]


class ReviewOutcome:
    def __init__(self, review: Review, node: TaskNode, artifact: Artifact) -> None:
        self.review = review
        self.node = node
        self.artifact = artifact

    @property
    def passed(self) -> bool:
        return self.review.status in ("PASSED", "CONDITIONAL")

    @property
    def reworkable(self) -> bool:
        return self.review.is_reworkable


class ReviewService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        agents: AgentRuntimePort,
        delegation: DelegationService,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._agents = agents
        self._delegation = delegation

    def review(self, task: Task, node: TaskNode, artifact: Artifact) -> ReviewOutcome:
        producer_id = self._producer_of(artifact)

        selection = self._delegation.select(
            required_capabilities=(node.review_policy.reviewer_capability,),
            required_permissions=("review.conduct",),
            exclude_agent_ids=(producer_id,),
            purpose=f"independently review {artifact.ref}",
        )
        reviewer = selection.agent
        reviewer_actor = reviewer.as_actor()
        producer = self._rt.repos.agents.get(producer_id)

        # The Review must exist before the node enters REVIEWING: the
        # `node_review_required` guard checks that a review was actually created for
        # this attempt, so that a node cannot claim to be under review with nobody
        # reviewing it.
        review = self._create_review(task, node, artifact, reviewer, producer_id, selection)

        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "start_review",
                actor=reviewer_actor,
                reason_code="REVIEW_REQUIRED",
                reason=(
                    f"policy requires {list(node.review_policy.review_types)} review; "
                    f"{reviewer.name} selected, producer {producer.name} excluded"
                ),
                correlation_id=task.id,
            ).entity
        )
        artifact = self._rt.repos.artifacts.save(
            self._engine.fire(
                artifact,
                "start_review",
                actor=reviewer_actor,
                reason_code="REVIEW_REQUIRED",
                reason=f"under review by {reviewer.name}",
                correlation_id=task.id,
            ).entity
        )

        review = self._rt.repos.reviews.save(
            self._engine.fire(
                review,
                "start",
                actor=reviewer_actor,
                reason_code="REVIEW_REQUIRED",
                reason=f"{reviewer.name} began round {review.round} review of {artifact.ref}",
                correlation_id=task.id,
            ).entity
        )

        opinion = self._agents.execute_review(
            reviewer.id,
            ReviewRequest(
                task_ref=task.ref,
                review_ref=review.ref,
                review_type=review.review_type.value,
                round=review.round,
                artifact=artifact,
                rubric=self._rt.policies.review_rubric(),
                previous_required_changes=self._previous_required_changes(node),
                node_objective=node.objective,
                node_name=node.name,
            ),
        )
        opinion = apply_evidence_source_rule(artifact, opinion)

        review = self._apply_opinion(review, opinion)
        derived = self._derive_verdict(review)

        if opinion.verdict != derived:
            return self._handle_inconsistent(task, node, artifact, review, opinion, derived)

        review = self._finalise(task, review, derived, reviewer_actor)
        node, artifact = self._settle(task, node, artifact, review, reviewer_actor)
        return ReviewOutcome(review, node, artifact)

    # ---- rework --------------------------------------------------------

    def request_rework(self, task: Task, node: TaskNode, review: Review) -> TaskNode:
        """Send the node back for another attempt.

        The failed review is untouched. ``attempt`` increments, which is what makes
        the next round's Review a distinct object rather than an edit of this one.
        """
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "rework",
                actor=owner.as_actor(),
                reason_code="REWORK_REQUESTED",
                reason=(
                    f"review {review.ref} round {review.round} FAILED; required changes: "
                    f"{list(review.required_changes)}"
                ),
                correlation_id=task.id,
                payload={
                    "failed_review": review.ref,
                    "round": review.round,
                    "required_changes": list(review.required_changes),
                },
            ).entity
        )
        # Close out the rejected attempt's assignment. CLOSED means "this assignment is
        # over", not "it succeeded" -- the review record is what says whether it did.
        self._close_rejected_assignment(task, review)

        node = self._rt.repos.nodes.get(node.id)
        node.attempt += 1
        return self._rt.repos.nodes.save(node)

    def _close_rejected_assignment(self, task: Task, review: Review) -> None:
        artifact = self._rt.repos.artifacts.get(review.artifact_id)
        if artifact.assignment_id is None:
            return
        assignment = self._rt.repos.assignments.get(artifact.assignment_id)
        if assignment.status != "SUBMITTED":
            return
        self._rt.repos.assignments.save(
            self._engine.fire(
                assignment,
                "close",
                actor=self._rt.repos.agents.get(task.owner_agent_id).as_actor(),  # type: ignore[arg-type]
                reason_code="REWORK_REQUESTED",
                reason=(
                    f"attempt {assignment.attempt} not accepted: review {review.ref} FAILED; "
                    f"superseded by a new attempt"
                ),
                correlation_id=task.id,
            ).entity
        )

    def escalate_exhausted(self, task: Task, node: TaskNode, review: Review) -> TaskNode:
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        return self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "attempts_exhausted",
                actor=owner.as_actor(),
                reason_code="ATTEMPTS_EXHAUSTED",
                reason=(
                    f"review {review.ref} FAILED at attempt {node.attempt}/{node.max_attempts}; "
                    "organization cannot resolve this without human input"
                ),
                correlation_id=task.id,
            ).entity
        )

    # ---- internals -----------------------------------------------------

    def _create_review(
        self,
        task: Task,
        node: TaskNode,
        artifact: Artifact,
        reviewer: Agent,
        producer_id: UUID,
        selection: Selection,
    ) -> Review:
        now = self._rt.clock.now()
        rid, ref = self._rt.mint("REV")
        previous = [r for r in self._rt.repos.reviews.all() if r.node_id == node.id]
        prior = sorted(previous, key=lambda r: r.round)[-1] if previous else None

        review = Review(
            id=rid,
            ref=ref,
            status="PENDING",
            created_at=now,
            created_by=reviewer.as_actor(),
            updated_at=now,
            task_id=task.id,
            node_id=node.id,
            artifact_id=artifact.id,
            review_type=ReviewType(node.review_policy.review_types[0]),
            reviewer_agent_id=reviewer.id,
            reviewer_role_id=reviewer.role_ids[0] if reviewer.role_ids else None,
            producer_agent_id=producer_id,
            round=node.attempt,
            rework_of_review_id=prior.id if prior else None,
            rubric=self._rt.policies.review_rubric(),
        )
        review = self._rt.repos.reviews.insert(review)
        self._engine.record_creation(
            review,
            actor=review.created_by,
            reason_code="REVIEW_REQUIRED",
            reason=selection.record.rationale,
            correlation_id=task.id,
            event_name="qor.review.created",
            payload={
                "artifact": artifact.ref,
                "round": review.round,
                "rework_of": str(prior.ref) if prior else None,
                "reviewer": reviewer.ref,
                "producer_excluded": True,
            },
        )
        return review

    def _apply_opinion(self, review: Review, opinion: ReviewOpinion) -> Review:
        review.findings = tuple(
            ReviewFinding(
                severity=f.severity,
                statement=f.statement,
                location=f.location,
                suggested_fix=f.suggested_fix,
            )
            for f in opinion.findings
        )
        review.conditions = tuple(ReviewCondition(statement=c) for c in opinion.conditions)
        review.required_changes = opinion.required_changes
        review.summary = opinion.summary
        return self._rt.repos.reviews.save(review)

    @staticmethod
    def _derive_verdict(review: Review) -> str:
        """The verdict the findings actually support."""
        if review.blocking_findings:
            return "FAILED"
        if review.unresolved_major:
            return "FAILED"
        if review.conditions:
            return "CONDITIONAL"
        return "PASSED"

    def _finalise(self, task: Task, review: Review, verdict: str, actor: ActorRef) -> Review:
        transition = {"PASSED": "pass", "FAILED": "fail", "CONDITIONAL": "conditional"}[verdict]
        reason_code = {
            "PASSED": "REVIEW_PASSED",
            "CONDITIONAL": "REVIEW_CONDITIONAL",
            "FAILED": self._failure_reason_code(review),
        }[verdict]
        detail = review.summary or f"round {review.round} verdict {verdict}"
        review.verdict = verdict
        review = self._rt.repos.reviews.save(review)
        return self._rt.repos.reviews.save(
            self._engine.fire(
                review,
                transition,
                actor=actor,
                reason_code=reason_code,
                reason=detail,
                correlation_id=task.id,
                payload={
                    "round": review.round,
                    "verdict": verdict,
                    "findings": [f"{f.severity}: {f.statement}" for f in review.findings],
                    "required_changes": list(review.required_changes),
                },
            ).entity
        )

    @staticmethod
    def _failure_reason_code(review: Review) -> str:
        """Pick the controlled reason code that matches why it failed.

        This is what makes `GROUP BY reason_code` useful later: "our reviews fail
        because evidence is thin" is a different organizational problem from
        "our reviews fail because methodology is unstated".
        """
        text = " ".join(f.statement.lower() for f in review.findings)
        if "evidence" in text or "sample" in text:
            return "REVIEW_FAILED_EVIDENCE_INSUFFICIENT"
        if "conflict" in text or "contradict" in text:
            return "REVIEW_FAILED_CONFLICTING_CONCLUSIONS"
        return "REVIEW_FAILED_METHODOLOGY"

    def _settle(
        self, task: Task, node: TaskNode, artifact: Artifact, review: Review, actor: ActorRef
    ) -> tuple[TaskNode, Artifact]:
        if review.status in ("PASSED", "CONDITIONAL"):
            artifact = self._rt.repos.artifacts.save(
                self._engine.fire(
                    artifact,
                    "accept",
                    actor=actor,
                    reason_code="ARTIFACT_ACCEPTED",
                    reason=f"review {review.ref} verdict {review.status}",
                    correlation_id=task.id,
                ).entity
            )
            node = self._rt.repos.nodes.save(
                self._engine.fire(
                    node,
                    "review_passed",
                    actor=actor,
                    reason_code="REVIEW_PASSED",
                    reason=(
                        f"{review.ref} round {review.round} {review.status}: "
                        f"{review.summary or 'no blocking findings'}"
                    ),
                    correlation_id=task.id,
                ).entity
            )
            self._close_assignment(task, artifact)
        return node, artifact

    def _close_assignment(self, task: Task, artifact: Artifact) -> None:
        if artifact.assignment_id is None:
            return
        assignment = self._rt.repos.assignments.get(artifact.assignment_id)
        if assignment.status != "SUBMITTED":
            return
        self._rt.repos.assignments.save(
            self._engine.fire(
                assignment,
                "close",
                actor=self._rt.repos.agents.get(task.owner_agent_id).as_actor(),  # type: ignore[arg-type]
                reason_code="REVIEW_PASSED",
                reason=f"{artifact.ref} accepted; assignment complete",
                correlation_id=task.id,
            ).entity
        )

    def _handle_inconsistent(
        self,
        task: Task,
        node: TaskNode,
        artifact: Artifact,
        review: Review,
        opinion: ReviewOpinion,
        derived: str,
    ) -> ReviewOutcome:
        """A reviewer whose verdict contradicts its own findings.

        Neither accepting nor silently overriding is acceptable, so the review is
        blocked and the node escalated to a human. This path should never fire with
        the V0.1 mock reviewer; it exists because the alternative is a silent
        governance hole.
        """
        actor = self._rt.repos.agents.get(review.reviewer_agent_id).as_actor()
        review = self._rt.repos.reviews.save(
            self._engine.fire(
                review,
                "block",
                actor=actor,
                reason_code="LOW_CONFIDENCE_DISAGREEMENT",
                reason=(
                    f"reviewer proposed '{opinion.verdict}' but findings support '{derived}'; "
                    "verdict inconsistent with evidence"
                ),
                correlation_id=task.id,
            ).entity
        )
        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "escalate",
                actor=actor,
                reason_code="LOW_CONFIDENCE_DISAGREEMENT",
                reason=f"review {review.ref} verdict inconsistent with its own findings",
                correlation_id=task.id,
            ).entity
        )
        return ReviewOutcome(review, node, artifact)

    def _producer_of(self, artifact: Artifact) -> UUID:
        if artifact.assignment_id is None:
            raise QORError(f"{artifact.ref} has no assignment; producer is unknown")
        return self._rt.repos.assignments.get(artifact.assignment_id).assignee_agent_id

    def _previous_required_changes(self, node: TaskNode) -> tuple[str, ...]:
        prior = [
            r
            for r in self._rt.repos.reviews.all()
            if r.node_id == node.id and r.round == node.attempt - 1
        ]
        changes: list[str] = []
        for review in prior:
            changes += list(review.required_changes)
        return tuple(dict.fromkeys(changes))


_ = FindingSeverity
