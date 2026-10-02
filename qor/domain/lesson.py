"""Lesson -- a Human-approved reusable judgment sourced from Outcomes.

A Lesson is not an Outcome, not a BindingConstraint, not Memory, and not a
policy. It may appear on PlanRequest as context. It never binds planning.
Human-validated subsequent evidence may be appended; that evidence is not a
lifecycle state and is not a claim that the Lesson caused a later result.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from qor.domain.base import Entity
from qor.kernel.errors import InvariantViolation

__all__ = [
    "LESSON_EVIDENCE_JUDGMENTS",
    "LESSON_EVIDENCE_USAGES",
    "LESSON_STATUSES",
    "Lesson",
    "LessonEvidence",
]

LESSON_STATUSES = frozenset({"CANDIDATE", "APPROVED", "WITHDRAWN", "SUPERSEDED"})
LESSON_EVIDENCE_JUDGMENTS = frozenset({"SUPPORTING", "CONTRADICTING", "UNCERTAIN"})
LESSON_EVIDENCE_USAGES = frozenset({"VISIBLE", "NOT_VISIBLE"})


class LessonEvidence(BaseModel):
    """One Human-validated subsequent evidence item. Not a verdict on the Lesson."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_outcome_id: UUID
    judgment: str
    usage: str
    recorded_by_agent_id: UUID
    recorded_at: datetime

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if self.judgment not in LESSON_EVIDENCE_JUDGMENTS:
            raise InvariantViolation(
                "I-L2",
                f"LessonEvidence.judgment '{self.judgment}' is not one of "
                f"{sorted(LESSON_EVIDENCE_JUDGMENTS)}",
            )
        if self.usage not in LESSON_EVIDENCE_USAGES:
            raise InvariantViolation(
                "I-L2",
                f"LessonEvidence.usage '{self.usage}' is not one of "
                f"{sorted(LESSON_EVIDENCE_USAGES)}",
            )
        return self


class Lesson(Entity):
    ENTITY_TYPE: ClassVar[str] = "Lesson"
    REF_PREFIX: ClassVar[str] = "LSN"

    statement: str
    applicability_subject_key: str
    applicability_decision_type: str = ""
    source_outcome_ids: tuple[UUID, ...]
    created_as: str = "CANDIDATE"
    approved_by_agent_id: UUID | None = None
    supersedes_lesson_id: UUID | None = None
    drafted_by_agent_id: UUID | None = None
    evidences: tuple[LessonEvidence, ...] = ()

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if not self.statement.strip():
            raise InvariantViolation("I-L1", "Lesson.statement must be non-empty")
        if not self.applicability_subject_key.strip():
            raise InvariantViolation("I-L1", "Lesson.applicability_subject_key must be non-empty")
        if not self.source_outcome_ids:
            raise InvariantViolation("I-L1", "Lesson must cite at least one source Outcome")
        if self.created_as != "CANDIDATE":
            raise InvariantViolation("I-L1", "Lesson.created_as must be CANDIDATE")
        if self.status not in LESSON_STATUSES:
            raise InvariantViolation(
                "I-L1", f"Lesson.status '{self.status}' is not one of {sorted(LESSON_STATUSES)}"
            )
        return self

    @property
    def evidence_view(self) -> str:
        """Derived read-only interpretation. Never a Lesson.status value."""
        if not self.evidences:
            return "UNEVALUATED"
        judgments = {item.judgment for item in self.evidences}
        supporting = "SUPPORTING" in judgments
        contradicting = "CONTRADICTING" in judgments
        if supporting and contradicting:
            return "UNCERTAIN"
        if judgments == {"SUPPORTING"}:
            return "HAS_SUPPORTING_EVIDENCE"
        if judgments == {"CONTRADICTING"}:
            return "HAS_CONTRADICTING_EVIDENCE"
        return "UNCERTAIN"
