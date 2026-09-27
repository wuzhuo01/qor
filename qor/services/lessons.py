"""Lesson draft / approve / withdraw / supersede / evaluate.

Planner never calls this service. An APPROVED Lesson is Human-validated context
about prior AUTHORITATIVE Outcomes. It is not a BindingConstraint, Memory, or
organizational fact. Human evaluate appends subsequent evidence only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from qor.domain.lesson import LESSON_EVIDENCE_JUDGMENTS, Lesson, LessonEvidence
from qor.domain.outcome import Outcome
from qor.kernel.errors import QORError
from qor.services.authorization import Authorizer
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

if TYPE_CHECKING:
    from qor.services.planning import PlanningService

__all__ = ["LessonService"]


class LessonService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        planning: PlanningService,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._planning = planning

    def draft(
        self,
        *,
        statement: str,
        applicability_subject_key: str,
        source_outcome_ids: tuple[UUID, ...],
        drafted_by_agent_id: UUID,
        applicability_decision_type: str = "",
        supersedes_lesson_id: UUID | None = None,
    ) -> Lesson:
        sources = self._resolve_sources(source_outcome_ids)
        self._require_applicability(applicability_subject_key, sources)
        drafter = self._rt.repos.agents.get(drafted_by_agent_id)
        actor = drafter.as_actor()
        lid, ref = self._rt.mint("LSN")
        now = self._rt.clock.now()
        lesson = Lesson(
            id=lid,
            ref=ref,
            status="CANDIDATE",
            created_at=now,
            created_by=actor,
            updated_at=now,
            statement=statement,
            applicability_subject_key=applicability_subject_key,
            applicability_decision_type=applicability_decision_type,
            source_outcome_ids=tuple(item.id for item in sources),
            created_as="CANDIDATE",
            drafted_by_agent_id=drafter.id,
            supersedes_lesson_id=supersedes_lesson_id,
        )
        self._authz.check(
            actor=actor,
            permission="lesson.draft",
            target_type="Lesson",
            target_id=lesson.id,
            target_ref=lesson.ref,
        )
        stored = self._rt.repos.lessons.insert(lesson)
        self._engine.record_creation(
            stored,
            actor=actor,
            reason_code="LESSON_DRAFTED",
            reason=(f"{drafter.name} drafted {stored.ref} from {[item.ref for item in sources]}"),
            event_name="qor.lesson.drafted",
            payload={
                "source_outcome_refs": [item.ref for item in sources],
                "applicability_subject_key": stored.applicability_subject_key,
            },
        )
        return stored

    def approve(self, lesson: Lesson, *, approved_by_agent_id: UUID) -> Lesson:
        current = self._rt.repos.lessons.get(lesson.id)
        self._resolve_sources(current.source_outcome_ids)
        self._require_unique_approved(current)
        actor = self._rt.repos.agents.get(approved_by_agent_id).as_actor()
        current.approved_by_agent_id = approved_by_agent_id
        stored = self._rt.repos.lessons.save(
            self._engine.fire(
                current,
                "approve",
                actor=actor,
                reason_code="LESSON_APPROVED",
                reason=f"{current.ref} approved as Planner context, not a constraint",
            ).entity
        )
        return stored

    def withdraw(self, lesson: Lesson, *, withdrawn_by_agent_id: UUID) -> Lesson:
        actor = self._rt.repos.agents.get(withdrawn_by_agent_id).as_actor()
        return self._rt.repos.lessons.save(
            self._engine.fire(
                lesson,
                "withdraw",
                actor=actor,
                reason_code="LESSON_WITHDRAWN",
                reason=f"{lesson.ref} withdrawn; it must no longer appear as Planner context",
            ).entity
        )

    def supersede(
        self,
        old: Lesson,
        *,
        statement: str,
        applicability_subject_key: str,
        source_outcome_ids: tuple[UUID, ...],
        approved_by_agent_id: UUID,
        applicability_decision_type: str = "",
    ) -> Lesson:
        actor = self._rt.repos.agents.get(approved_by_agent_id).as_actor()
        self._rt.repos.lessons.save(
            self._engine.fire(
                old,
                "supersede",
                actor=actor,
                reason_code="LESSON_SUPERSEDED",
                reason=f"{old.ref} superseded by a later Lesson",
            ).entity
        )
        drafted = self.draft(
            statement=statement,
            applicability_subject_key=applicability_subject_key,
            source_outcome_ids=source_outcome_ids,
            drafted_by_agent_id=approved_by_agent_id,
            applicability_decision_type=applicability_decision_type,
            supersedes_lesson_id=old.id,
        )
        return self.approve(drafted, approved_by_agent_id=approved_by_agent_id)

    def evaluate(
        self,
        lesson: Lesson,
        *,
        source_outcome_id: UUID,
        judgment: str,
        recorded_by_agent_id: UUID,
    ) -> Lesson:
        current = self._rt.repos.lessons.get(lesson.id)
        if current.status != "APPROVED":
            raise QORError(
                f"{current.ref} is {current.status}; only APPROVED Lesson can be evaluated"
            )
        if judgment not in LESSON_EVIDENCE_JUDGMENTS:
            raise QORError(
                f"judgment '{judgment}' is not one of {sorted(LESSON_EVIDENCE_JUDGMENTS)}"
            )
        if not self._live_source_ids(current):
            raise QORError(
                f"{current.ref} has no live AUTHORITATIVE source Outcome; "
                "new evidence cannot be recorded"
            )
        outcome = self._rt.repos.outcomes.find(source_outcome_id)
        if outcome is None:
            raise QORError(f"Outcome {source_outcome_id} not found")
        if outcome.status != "AUTHORITATIVE":
            raise QORError(
                f"{outcome.ref} is {outcome.status}; only AUTHORITATIVE Outcome "
                "can be Lesson evidence"
            )
        if outcome.id in current.source_outcome_ids:
            raise QORError(
                f"{outcome.ref} sourced {current.ref}; a Lesson cannot be evaluated "
                "by the Outcome that created it"
            )
        if outcome.subject_key != current.applicability_subject_key:
            raise QORError(
                f"{outcome.ref} subject_key '{outcome.subject_key}' does not exactly "
                f"match Lesson applicability '{current.applicability_subject_key}'"
            )
        usage = self._usage_for(current, outcome)
        if judgment in {"SUPPORTING", "CONTRADICTING"} and usage != "VISIBLE":
            raise QORError(
                f"{judgment} evidence requires the later Task PlanRequest to contain "
                f"{current.ref} in prior_lessons"
            )
        recorder = self._rt.repos.agents.get(recorded_by_agent_id)
        actor = recorder.as_actor()
        self._authz.check(
            actor=actor,
            permission="lesson.evaluate",
            target_type="Lesson",
            target_id=current.id,
            target_ref=current.ref,
        )
        evidence = LessonEvidence(
            source_outcome_id=outcome.id,
            judgment=judgment,
            usage=usage,
            recorded_by_agent_id=recorder.id,
            recorded_at=self._rt.clock.now(),
        )
        current.evidences = (*current.evidences, evidence)
        current.updated_at = evidence.recorded_at
        stored = self._rt.repos.lessons.save(current)
        self._engine.emit(
            "qor.lesson.evaluated",
            entity=stored,
            actor=actor,
            payload={
                "source_outcome_ref": outcome.ref,
                "judgment": judgment,
                "usage": usage,
                "lesson_status": stored.status,
            },
            correlation_id=stored.id,
        )
        return stored

    def _usage_for(self, lesson: Lesson, outcome: Outcome) -> str:
        task = self._rt.repos.tasks.find(outcome.task_id)
        if task is None:
            return "NOT_VISIBLE"
        request = self._planning.build_request(task)
        if any(item.ref == lesson.ref for item in request.prior_lessons):
            return "VISIBLE"
        return "NOT_VISIBLE"

    def _live_source_ids(self, lesson: Lesson) -> tuple[UUID, ...]:
        live: list[UUID] = []
        for oid in lesson.source_outcome_ids:
            outcome = self._rt.repos.outcomes.find(oid)
            if outcome is not None and outcome.status == "AUTHORITATIVE":
                live.append(oid)
        return tuple(live)

    def _resolve_sources(self, source_outcome_ids: tuple[UUID, ...]) -> tuple[Outcome, ...]:
        if not source_outcome_ids:
            raise QORError("Lesson must cite at least one AUTHORITATIVE Outcome")
        sources: list[Outcome] = []
        for oid in source_outcome_ids:
            outcome = self._rt.repos.outcomes.find(oid)
            if outcome is None:
                raise QORError(f"Outcome {oid} not found")
            if outcome.status != "AUTHORITATIVE":
                raise QORError(
                    f"{outcome.ref} is {outcome.status}; only AUTHORITATIVE Outcome "
                    "can be Lesson provenance"
                )
            sources.append(outcome)
        return tuple(sources)

    @staticmethod
    def _require_applicability(subject_key: str, sources: tuple[Outcome, ...]) -> None:
        allowed = {item.subject_key for item in sources}
        if subject_key not in allowed:
            raise QORError(
                f"Lesson applicability_subject_key '{subject_key}' is not an exact "
                f"source Outcome subject_key {sorted(allowed)}"
            )

    def _require_unique_approved(self, lesson: Lesson) -> None:
        for other in self._rt.repos.lessons.all():
            if other.id == lesson.id or other.status != "APPROVED":
                continue
            if (
                other.applicability_subject_key == lesson.applicability_subject_key
                and other.applicability_decision_type == lesson.applicability_decision_type
            ):
                raise QORError(
                    f"{other.ref} is already APPROVED for this applicability; "
                    "Human must supersede rather than approve a second Lesson"
                )
