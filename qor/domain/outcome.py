"""Outcome -- a recorded fact about a decided Decision.

V0.3's load-bearing object. A past Decision does not constrain the next similar
Task by sitting in a log; it constrains it when someone accountable records what
actually happened, and that record is injected as an organization fact (and, when
it is CONTRADICTED + HIGH + AUTHORITATIVE, as a binding constraint).

Planner does not create Outcomes. An outside event does not create Outcomes. Human CIO
(or an authorized recorder) does.
"""

from __future__ import annotations

from typing import ClassVar, Self
from uuid import UUID

from pydantic import model_validator

from qor.domain.base import Entity
from qor.kernel.errors import InvariantViolation

__all__ = [
    "FOLLOW_THROUGH_FULFILLED",
    "FOLLOW_THROUGH_KIND",
    "FOLLOW_THROUGH_OPEN",
    "FOLLOW_THROUGH_WAIVED",
    "OUTCOME_CONFIDENCES",
    "OUTCOME_STATUSES",
    "OUTCOME_VERDICTS",
    "Outcome",
]

OUTCOME_VERDICTS = frozenset({"CONTRADICTED", "CONFIRMED", "UNCERTAIN"})
OUTCOME_CONFIDENCES = frozenset({"HIGH", "LOW"})
OUTCOME_STATUSES = frozenset({"AUTHORITATIVE", "WITHDRAWN", "SUPERSEDED"})
FOLLOW_THROUGH_KIND = "FOLLOW_THROUGH_CONTRADICTED_OUTCOME"
FOLLOW_THROUGH_OPEN = "OPEN"
FOLLOW_THROUGH_WAIVED = "WAIVED"
FOLLOW_THROUGH_FULFILLED = "FULFILLED"
FOLLOW_THROUGH_STATUSES = frozenset(
    {"", FOLLOW_THROUGH_OPEN, FOLLOW_THROUGH_WAIVED, FOLLOW_THROUGH_FULFILLED}
)


class Outcome(Entity):
    ENTITY_TYPE: ClassVar[str] = "Outcome"
    REF_PREFIX: ClassVar[str] = "OUT"

    decision_id: UUID
    task_id: UUID
    source_action_id: UUID | None = None
    subject_key: str
    verdict: str
    contradicted_decision_type: str
    confidence: str
    statement: str = ""
    supersedes_outcome_id: UUID | None = None
    recorded_by_agent_id: UUID | None = None
    follow_through_kind: str = ""
    follow_through_status: str = ""
    follow_through_task_id: UUID | None = None
    measurement_metrics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if not self.subject_key.strip():
            raise InvariantViolation("I-O1", "Outcome.subject_key must be non-empty")
        if self.verdict not in OUTCOME_VERDICTS:
            raise InvariantViolation(
                "I-O1", f"Outcome.verdict '{self.verdict}' is not one of {sorted(OUTCOME_VERDICTS)}"
            )
        if self.confidence not in OUTCOME_CONFIDENCES:
            raise InvariantViolation(
                "I-O1",
                f"Outcome.confidence '{self.confidence}' is not one of {sorted(OUTCOME_CONFIDENCES)}",
            )
        if not self.contradicted_decision_type.strip():
            raise InvariantViolation("I-O1", "Outcome.contradicted_decision_type must be non-empty")
        if self.follow_through_status not in FOLLOW_THROUGH_STATUSES:
            raise InvariantViolation(
                "I-O1",
                f"Outcome.follow_through_status '{self.follow_through_status}' "
                f"is not one of {sorted(FOLLOW_THROUGH_STATUSES)}",
            )
        if self.follow_through_kind and self.follow_through_kind != FOLLOW_THROUGH_KIND:
            raise InvariantViolation(
                "I-O1",
                f"Outcome.follow_through_kind '{self.follow_through_kind}' is not "
                f"{FOLLOW_THROUGH_KIND}",
            )
        if self.status == "AUTHORITATIVE" and not self.measurement_metrics:
            raise InvariantViolation(
                "I-O2",
                "AUTHORITATIVE Outcome must cite at least one measurement metric",
            )
        return self
