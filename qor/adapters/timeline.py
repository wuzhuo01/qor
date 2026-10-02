"""Organization Timeline -- a human-readable projection of the event log.

Ruling B7: the display names from the V0.1 requirement (``TASK_CREATED`` and so on)
are kept as a *presentation* layer over the namespaced events. They are the same
data, not a second source of truth, and anything without a mapping still shows up
rather than being silently dropped.

The timeline is also where the ten audit questions from requirement §13 are
answered, in ``answer_audit_questions``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from qor.adapters.persistence.inmemory import InMemoryLogStore
from qor.domain.audit import Event
from qor.services.runtime import Runtime

__all__ = ["Timeline", "TimelineEntry"]

DISPLAY_NAMES = {
    "qor.task.created": "TASK_CREATED",
    "qor.task.opened": "TASK_OPENED",
    "qor.task.planning_started": "TASK_PLANNING",
    "qor.plan.created": "TASK_PLAN_CREATED",
    "qor.plan.proposed": "PLAN_PROPOSED",
    "qor.plan.approved": "PLAN_APPROVED",
    "qor.plan.activated": "PLAN_ACTIVATED",
    "qor.node.created": "TASK_NODE_CREATED",
    "qor.task.ready": "TASK_READY",
    "qor.task.started": "TASK_RUNNING",
    "qor.node.ready": "TASK_NODE_READY",
    "qor.node.assigning": "CAPABILITY_MATCHING",
    "qor.delegation.agent_selected": "AGENT_SELECTED",
    "qor.assignment.offered": "ASSIGNMENT_CREATED",
    "qor.assignment.accepted": "ASSIGNMENT_ACCEPTED",
    "qor.node.assigned": "TASK_NODE_ASSIGNED",
    "qor.assignment.started": "AGENT_STARTED",
    "qor.node.started": "TASK_NODE_RUNNING",
    "qor.artifact.created": "ARTIFACT_CREATED",
    "qor.artifact.submitted": "ARTIFACT_SUBMITTED",
    "qor.assignment.submitted": "AGENT_COMPLETED",
    "qor.node.submitted": "TASK_NODE_SUBMITTED",
    "qor.node.review_started": "TASK_NODE_REVIEWING",
    "qor.artifact.under_review": "ARTIFACT_UNDER_REVIEW",
    "qor.review.created": "REVIEW_CREATED",
    "qor.review.started": "REVIEW_STARTED",
    "qor.review.passed": "REVIEW_PASSED",
    "qor.review.failed": "REVIEW_FAILED",
    "qor.review.conditional": "REVIEW_CONDITIONAL",
    "qor.node.rework": "REWORK_REQUESTED",
    "qor.artifact.superseded": "ARTIFACT_SUPERSEDED",
    "qor.artifact.accepted": "ARTIFACT_ACCEPTED",
    "qor.node.accepted": "TASK_NODE_ACCEPTED",
    "qor.assignment.closed": "ASSIGNMENT_CLOSED",
    "qor.task.review_started": "TASK_REVIEW_GATE",
    "qor.task.synthesizing": "SYNTHESIS_STARTED",
    "qor.task.deciding": "TASK_DECIDING",
    "qor.decision.created": "DECISION_CREATED",
    "qor.decision.proposed": "DECISION_PROPOSED",
    "qor.decision.decided": "DECISION_MADE",
    "qor.action.requested": "ACTION_REQUESTED",
    "qor.action.permission_check": "ACTION_PERMISSION_CHECK",
    "qor.action.risk_check": "ACTION_RISK_CHECK",
    "qor.action.approval_pending": "ACTION_APPROVAL_CHECK",
    "qor.action.approved": "ACTION_APPROVED",
    "qor.action.stopped_at_policy_stop_state": "ACTION_STOPPED_DRY_RUN",
    "qor.task.action_pending": "TASK_ACTION_PENDING",
    "qor.task.completed": "TASK_COMPLETED",
    "qor.task.blocked": "TASK_BLOCKED",
    "qor.node.blocked": "TASK_NODE_BLOCKED",
    "qor.delegation.no_capable_agent": "CAPABILITY_GAP",
    "qor.node.escalated": "TASK_NODE_ESCALATED",
    "qor.task.escalated": "TASK_ESCALATED",
    "qor.agent.available": "AGENT_AVAILABLE",
    "qor.agent.busy": "AGENT_BUSY",
    "qor.agent.offline": "AGENT_OFFLINE",
}


@dataclass(frozen=True)
class TimelineEntry:
    seq: int
    display: str
    event: str
    subject: str
    detail: str

    def __str__(self) -> str:
        line = f"{self.seq:>4}  {self.display:<26} {self.subject}"
        return f"{line}\n          {self.detail}" if self.detail else line


class Timeline:
    def __init__(self, log: InMemoryLogStore) -> None:
        self._log = log

    def entries(self, *, correlation_id: UUID | None = None) -> tuple[TimelineEntry, ...]:
        events = [
            e
            for e in self._log.events()
            if correlation_id is None or e.correlation_id == correlation_id
        ]
        return tuple(
            TimelineEntry(
                seq=event.seq,
                display=DISPLAY_NAMES.get(event.name, event.name.upper().replace(".", "_")),
                event=event.name,
                subject=f"{event.aggregate_ref} ({event.aggregate_type})",
                detail=self._detail(event),
            )
            for event in events
        )

    def render(self, *, correlation_id: UUID | None = None) -> str:
        return "\n".join(str(entry) for entry in self.entries(correlation_id=correlation_id))

    @staticmethod
    def _detail(event: Event) -> str:
        payload = event.payload
        interesting = (
            "reason_code",
            "objective",
            "rationale",
            "required_capabilities",
            "selected_name",
            "score",
            "verdict",
            "round",
            "required_changes",
            "statement",
            "decision_type",
            "operation",
            "environment",
            "dry_run",
            "execute_reachable",
            "blocked_by",
            "checks",
            "type",
            "findings",
            "node_kind",
        )
        parts = [
            f"{key}={payload[key]}"
            for key in interesting
            if key in payload and payload[key] not in (None, "", [], {})
        ]
        return "  ".join(parts)

    # ---- the ten audit questions from requirement §13 -------------------

    def answer_audit_questions(self, runtime: Runtime, task_id: UUID) -> list[tuple[str, str]]:
        """Answer each of the ten questions purely from persisted records."""
        rt = runtime
        task = rt.repos.tasks.get(task_id)
        plan = rt.repos.plans.get(task.active_plan_id) if task.active_plan_id else None
        nodes = [rt.repos.nodes.get(n) for n in (plan.node_ids if plan else ())]
        artifacts = [a for a in rt.repos.artifacts.all() if a.task_id == task_id]
        reviews = sorted(
            (r for r in rt.repos.reviews.all() if r.task_id == task_id),
            key=lambda r: (r.node_id.hex if r.node_id else "", r.round),
        )
        decisions = [d for d in rt.repos.decisions.all() if d.task_id == task_id]
        assignments = [a for a in rt.repos.assignments.all() if a.task_id == task_id]

        def agent_name(agent_id: UUID) -> str:
            return rt.repos.agents.get(agent_id).name

        answers: list[tuple[str, str]] = []
        answers.append(("1. What is this task?", f"{task.ref} [{task.status}] {task.objective}"))
        answers.append(
            (
                "2. Why did the AI CIO split it this way?",
                plan.decision_rationale if plan else "no plan",
            )
        )
        answers.append(
            (
                "3. What capability does each node require?",
                "; ".join(f"{n.ref} {n.name} -> {list(n.required_capabilities)}" for n in nodes),
            )
        )
        answers.append(
            (
                "4. Why was each agent chosen?",
                "\n".join(
                    f"     {a.ref} -> {a.selection.rationale}"
                    for a in sorted(assignments, key=lambda x: x.ref)
                ),
            )
        )
        answers.append(
            (
                "5. What artifacts were produced?",
                "; ".join(
                    f"{a.ref} {a.artifact_type.value} v{a.artifact_version} [{a.status}] "
                    f"by {agent_name(rt.repos.assignments.get(a.assignment_id).assignee_agent_id)}"
                    for a in sorted(artifacts, key=lambda x: x.ref)
                    if a.assignment_id
                ),
            )
        )
        answers.append(
            (
                "6. Who reviewed each artifact?",
                "; ".join(
                    f"{r.ref} round {r.round}: {agent_name(r.reviewer_agent_id)} reviewed "
                    f"{rt.repos.artifacts.get(r.artifact_id).ref} "
                    f"(producer {agent_name(r.producer_agent_id)}, independent)"
                    for r in reviews
                ),
            )
        )
        answers.append(
            (
                "7. Did the reviews pass?",
                "; ".join(f"{r.ref} round {r.round} = {r.status}" for r in reviews),
            )
        )
        recommendation = next(
            (a for a in artifacts if a.artifact_type.value == "RECOMMENDATION"), None
        )
        answers.append(
            (
                "8. What is the final recommendation?",
                recommendation.findings[0].statement
                if recommendation and recommendation.findings
                else "none",
            )
        )
        answers.append(
            (
                "9. What is the final decision?",
                "; ".join(
                    f"{d.ref} [{d.status}] {d.decision_type.value}: {d.statement}"
                    for d in decisions
                ),
            )
        )
        answers.append(
            (
                "10. Why is the task considered complete?",
                task.completion_rationale or "not complete",
            )
        )
        return answers
