"""Review -- the only quality exit (invariant I5).

Two rules do the real work here:

* **Four-Eyes**: reviewer != producer. Checked as a guard, not a convention.
* **Terminal states are irreversible** (ruling C4/E2): a failed review is never
  reopened. Rework creates round n+1 linked by ``rework_of_review_id``, so "why
  did round 1 fail" survives forever.
"""

from __future__ import annotations

from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from qor.domain.base import Entity
from qor.domain.enums import FindingSeverity, ReviewType
from qor.kernel.errors import InvariantViolation

__all__ = ["Review", "ReviewCondition", "ReviewFinding"]


class ReviewFinding(BaseModel):
    """A defect the reviewer found, with the severity that drives the verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: FindingSeverity
    statement: str
    location: str = ""
    suggested_fix: str = ""

    @model_validator(mode="after")
    def _statement_present(self) -> Self:
        if not self.statement.strip():
            raise InvariantViolation("I5", "ReviewFinding.statement must be non-empty")
        return self


class ReviewCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str
    must_satisfy_before: str = ""


class Review(Entity):
    ENTITY_TYPE: ClassVar[str] = "Review"
    REF_PREFIX: ClassVar[str] = "REV"

    task_id: UUID
    node_id: UUID | None = None
    artifact_id: UUID
    review_type: ReviewType
    reviewer_agent_id: UUID
    reviewer_role_id: UUID | None = None
    producer_agent_id: UUID

    round: int = 1
    rework_of_review_id: UUID | None = None

    findings: tuple[ReviewFinding, ...] = ()
    conditions: tuple[ReviewCondition, ...] = ()
    required_changes: tuple[str, ...] = ()
    verdict: str | None = None
    rubric: tuple[str, ...] = ()
    summary: str = ""

    @model_validator(mode="after")
    def _four_eyes(self) -> Self:
        """Invariant I5: an agent cannot review its own work."""
        if self.reviewer_agent_id == self.producer_agent_id:
            raise InvariantViolation(
                "I5",
                f"reviewer and producer are the same agent ({self.reviewer_agent_id}); "
                "Four-Eyes Principle violated",
            )
        return self

    @property
    def blocking_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.BLOCKER)

    @property
    def unresolved_major(self) -> tuple[ReviewFinding, ...]:
        return tuple(f for f in self.findings if f.severity is FindingSeverity.MAJOR)

    @property
    def is_reworkable(self) -> bool:
        """A FAILED review can be reworked only if it said what to change.

        "Failed, figure it out" is not actionable and would let the organization
        loop without learning anything.
        """
        return self.verdict == "FAILED" and bool(self.required_changes)
