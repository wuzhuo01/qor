"""Outcome recording -- Human CIO (or authorized recorder) only.

The planner never calls this service. An Outcome is a fact about a Decision that
has already been DECIDED; inventing one during planning would let the planner
write the constraint it is supposed to obey.

V0.4: a qualified CONTRADICTED + HIGH + AUTHORITATIVE record also opens
Follow-through through Intake. That is organizational governance, not a
workflow hop and not an Obligation aggregate.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.action import Action
from qor.domain.decision import Decision
from qor.domain.outcome import (
    FOLLOW_THROUGH_FULFILLED,
    FOLLOW_THROUGH_KIND,
    FOLLOW_THROUGH_OPEN,
    FOLLOW_THROUGH_WAIVED,
    Outcome,
)
from qor.kernel.actor import ActorRef
from qor.kernel.errors import QORError
from qor.services.authorization import Authorizer
from qor.services.intake import IntakeService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["OutcomeService"]


class OutcomeService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        intake: IntakeService,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._intake = intake

    def record(
        self,
        *,
        decision_id: UUID,
        subject_key: str,
        verdict: str,
        contradicted_decision_type: str,
        confidence: str,
        statement: str,
        recorded_by_agent_id: UUID,
        measurement_metrics: tuple[str, ...],
        supersedes_outcome_id: UUID | None = None,
        source_action_id: UUID | None = None,
        source_action_ref: str | None = None,
    ) -> Outcome:
        decision = self._rt.repos.decisions.get(decision_id)
        if decision.status != "DECIDED":
            raise QORError(
                f"Outcome must be recorded against a DECIDED Decision; "
                f"{decision.ref} is {decision.status}"
            )
        self._require_measurement_binding(decision, measurement_metrics)
        recorder = self._rt.repos.agents.get(recorded_by_agent_id)
        actor = recorder.as_actor()
        oid, ref = self._rt.mint("OUT")
        now = self._rt.clock.now()
        outcome = Outcome(
            id=oid,
            ref=ref,
            status="AUTHORITATIVE",
            created_at=now,
            created_by=actor,
            updated_at=now,
            decision_id=decision.id,
            task_id=decision.task_id,
            source_action_id=None,
            subject_key=subject_key,
            verdict=verdict,
            contradicted_decision_type=contradicted_decision_type,
            confidence=confidence,
            statement=statement,
            supersedes_outcome_id=supersedes_outcome_id,
            recorded_by_agent_id=recorder.id,
            measurement_metrics=measurement_metrics,
        )
        self._authz.check(
            actor=actor,
            permission="outcome.record",
            target_type="Outcome",
            target_id=outcome.id,
            target_ref=outcome.ref,
            correlation_id=decision.task_id,
        )
        outcome.source_action_id = self._bind_source_action(
            decision,
            source_action_id=source_action_id,
            source_action_ref=source_action_ref,
        )
        stored = self._rt.repos.outcomes.insert(outcome)
        self._engine.record_creation(
            stored,
            actor=actor,
            reason_code="OUTCOME_RECORDED",
            reason=(
                f"{recorder.name} recorded {stored.verdict}/{stored.confidence} "
                f"on {stored.subject_key} for {decision.ref}"
            ),
            correlation_id=decision.task_id,
            event_name="qor.outcome.recorded",
            payload={
                "decision_ref": decision.ref,
                "subject_key": stored.subject_key,
                "verdict": stored.verdict,
                "confidence": stored.confidence,
            },
        )
        return self._open_follow_through_if_required(stored, recorder_id=recorder.id)

    def withdraw(self, outcome: Outcome, *, withdrawn_by_agent_id: UUID) -> Outcome:
        actor = self._rt.repos.agents.get(withdrawn_by_agent_id).as_actor()
        stored = self._rt.repos.outcomes.save(
            self._engine.fire(
                outcome,
                "withdraw",
                actor=actor,
                reason_code="OUTCOME_WITHDRAWN",
                reason=f"{outcome.ref} withdrawn; it must no longer bind later plans",
                correlation_id=outcome.task_id,
            ).entity
        )
        return self._release_follow_through(
            stored,
            actor=actor,
            status="",
            reason_code="OUTCOME_WITHDRAWN",
            reason=f"{stored.ref} withdrawn; follow-through is no longer required",
        )

    def supersede(
        self,
        old: Outcome,
        *,
        subject_key: str,
        verdict: str,
        contradicted_decision_type: str,
        confidence: str,
        statement: str,
        recorded_by_agent_id: UUID,
        measurement_metrics: tuple[str, ...],
    ) -> Outcome:
        actor = self._rt.repos.agents.get(recorded_by_agent_id).as_actor()
        stored_old = self._rt.repos.outcomes.save(
            self._engine.fire(
                old,
                "supersede",
                actor=actor,
                reason_code="OUTCOME_SUPERSEDED",
                reason=f"{old.ref} superseded by a later recording",
                correlation_id=old.task_id,
            ).entity
        )
        self._release_follow_through(
            stored_old,
            actor=actor,
            status="",
            reason_code="OUTCOME_SUPERSEDED",
            reason=f"{stored_old.ref} superseded; its follow-through is no longer required",
        )
        return self.record(
            decision_id=old.decision_id,
            subject_key=subject_key,
            verdict=verdict,
            contradicted_decision_type=contradicted_decision_type,
            confidence=confidence,
            statement=statement,
            recorded_by_agent_id=recorded_by_agent_id,
            measurement_metrics=measurement_metrics,
            supersedes_outcome_id=old.id,
            source_action_id=old.source_action_id,
        )

    @staticmethod
    def _require_measurement_binding(
        decision: Decision, measurement_metrics: tuple[str, ...]
    ) -> None:
        if not measurement_metrics:
            raise QORError(
                f"{decision.ref} Outcome must cite at least one measurement metric "
                f"from the Decision's measurement plan"
            )
        plan = decision.measurement_plan
        if plan is None or not plan.metrics:
            raise QORError(f"{decision.ref} has no measurement_plan to bind")
        allowed = set(plan.metrics)
        unknown = [metric for metric in measurement_metrics if metric not in allowed]
        if unknown:
            raise QORError(
                f"{decision.ref} Outcome cites metrics not on the Decision measurement "
                f"plan: {unknown}"
            )

    def _actions_for_decision(self, decision_id: UUID) -> tuple[Action, ...]:
        return tuple(
            item for item in self._rt.repos.actions.all() if item.decision_id == decision_id
        )

    def _resolve_cited_action(
        self,
        source_action_id: UUID | None,
        source_action_ref: str | None,
    ) -> Action | None:
        cited_ref = (source_action_ref or "").strip() or None
        if source_action_id is None and cited_ref is None:
            return None
        if source_action_id is not None:
            action = self._rt.repos.actions.find(source_action_id)
            if action is None:
                raise QORError(f"Action {source_action_id} not found")
            if cited_ref is not None and action.ref != cited_ref:
                raise QORError(f"{action.ref} does not match source_action_ref {cited_ref}")
            return action
        matches = [item for item in self._rt.repos.actions.all() if item.ref == cited_ref]
        if len(matches) != 1:
            raise QORError(f"Action ref {cited_ref} is not a unique persisted Action")
        return matches[0]

    def _require_settled_verification(self, action: Action, decision: Decision) -> None:
        if action.decision_id != decision.id:
            raise QORError(
                f"{action.ref} is not an Action of {decision.ref} and cannot be Outcome provenance"
            )
        if action.status != "SETTLED":
            raise QORError(
                f"{action.ref} is {action.status}; only SETTLED Action can be Outcome provenance"
            )
        verification = action.verification
        if verification is None:
            raise QORError(f"{action.ref} has no verification and cannot be Outcome provenance")
        if verification.matched is not True:
            raise QORError(f"{action.ref} verification.matched is not True")
        if not verification.expected:
            raise QORError(f"{action.ref} verification.expected is missing")
        if not verification.observed:
            raise QORError(f"{action.ref} verification.observed is missing")

    def _bind_source_action(
        self,
        decision: Decision,
        *,
        source_action_id: UUID | None,
        source_action_ref: str | None,
    ) -> UUID | None:
        cited = self._resolve_cited_action(source_action_id, source_action_ref)
        settled = [
            item for item in self._actions_for_decision(decision.id) if item.status == "SETTLED"
        ]
        if cited is None:
            if settled:
                raise QORError(
                    f"{decision.ref} has SETTLED Action provenance that Outcome.record must cite"
                )
            return None
        self._require_settled_verification(cited, decision)
        return cited.id

    def waive_follow_through(
        self,
        outcome: Outcome,
        *,
        waived_by_agent_id: UUID,
        reason: str,
    ) -> Outcome:
        if not reason.strip():
            raise QORError("follow-through waive reason must be non-empty")
        current = self._rt.repos.outcomes.get(outcome.id)
        if current.follow_through_status != FOLLOW_THROUGH_OPEN:
            raise QORError(f"{current.ref} has no OPEN follow-through to waive")
        actor = self._rt.repos.agents.get(waived_by_agent_id).as_actor()
        return self._release_follow_through(
            current,
            actor=actor,
            status=FOLLOW_THROUGH_WAIVED,
            reason_code="FOLLOW_THROUGH_WAIVED",
            reason=reason,
        )

    def _open_follow_through_if_required(self, outcome: Outcome, *, recorder_id: UUID) -> Outcome:
        if not _qualifies(outcome):
            return outcome
        existing = [
            task for task in self._rt.repos.tasks.all() if task.source_outcome_id == outcome.id
        ]
        if existing or outcome.follow_through_status in {
            FOLLOW_THROUGH_OPEN,
            FOLLOW_THROUGH_FULFILLED,
        }:
            return outcome
        follow_up = self._intake.open_from_outcome(outcome, recorded_by_agent_id=recorder_id)
        outcome.follow_through_kind = FOLLOW_THROUGH_KIND
        outcome.follow_through_status = FOLLOW_THROUGH_OPEN
        outcome.follow_through_task_id = follow_up.id
        stored = self._rt.repos.outcomes.save(outcome)
        self._engine.emit(
            "qor.follow_through.required",
            entity=stored,
            actor=ActorRef.system("QOR-FOLLOW-THROUGH"),
            payload={
                "outcome_ref": stored.ref,
                "decision_ref": self._rt.repos.decisions.get(stored.decision_id).ref,
                "subject_key": stored.subject_key,
                "kind": FOLLOW_THROUGH_KIND,
                "status": FOLLOW_THROUGH_OPEN,
                "task_id": str(follow_up.id),
            },
            correlation_id=follow_up.id,
        )
        return stored

    def _release_follow_through(
        self,
        outcome: Outcome,
        *,
        actor: ActorRef,
        status: str,
        reason_code: str,
        reason: str,
    ) -> Outcome:
        if outcome.follow_through_status == FOLLOW_THROUGH_OPEN:
            self._cancel_follow_up(outcome, actor=actor, reason_code=reason_code, reason=reason)
            outcome.follow_through_status = status
            return self._rt.repos.outcomes.save(outcome)
        return outcome

    def _note_decision_addressed(self, decision: Decision) -> Outcome | None:
        """Close an OPEN Follow-through when a DECIDED Decision structurally addresses it.

        Not a public Agent/Human API. Planner and AI CIO cannot call this.
        """
        if decision.status != "DECIDED" or not decision.addresses_outcome_ref.strip():
            return None
        task = self._rt.repos.tasks.get(decision.task_id)
        if task.source_outcome_id is None:
            return None
        outcome = self._rt.repos.outcomes.get(task.source_outcome_id)
        if outcome.follow_through_status == FOLLOW_THROUGH_FULFILLED:
            return outcome
        if outcome.follow_through_status != FOLLOW_THROUGH_OPEN:
            return None
        if outcome.status != "AUTHORITATIVE":
            return None
        if outcome.follow_through_task_id != task.id:
            return None
        if decision.addresses_outcome_ref != outcome.ref:
            return None
        outcome.follow_through_status = FOLLOW_THROUGH_FULFILLED
        stored = self._rt.repos.outcomes.save(outcome)
        self._engine.emit(
            "qor.follow_through.fulfilled",
            entity=stored,
            actor=ActorRef.system("QOR-FOLLOW-THROUGH"),
            payload={
                "outcome_ref": stored.ref,
                "decision_ref": decision.ref,
                "task_id": str(task.id),
                "task_ref": task.ref,
                "addresses_outcome_ref": decision.addresses_outcome_ref,
                "reason_code": "FOLLOW_THROUGH_FULFILLED",
                "kind": FOLLOW_THROUGH_KIND,
                "status": FOLLOW_THROUGH_FULFILLED,
            },
            correlation_id=task.id,
        )
        return stored

    def _cancel_follow_up(
        self,
        outcome: Outcome,
        *,
        actor: ActorRef,
        reason_code: str,
        reason: str,
    ) -> None:
        if outcome.follow_through_task_id is None:
            return
        task = self._rt.repos.tasks.get(outcome.follow_through_task_id)
        machine = self._rt.machines.for_entity("Task")
        if machine.is_terminal(task.status):
            return
        self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "cancel",
                actor=actor,
                reason_code=reason_code,
                reason=reason,
                correlation_id=task.id,
                metadata={"outcome_ref": outcome.ref},
            ).entity
        )


def _qualifies(outcome: Outcome) -> bool:
    return (
        outcome.status == "AUTHORITATIVE"
        and outcome.verdict == "CONTRADICTED"
        and outcome.confidence == "HIGH"
    )
