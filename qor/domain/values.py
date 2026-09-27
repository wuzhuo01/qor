"""Kernel value objects: evidence, confidence, uncertainty, constraints.

These exist as first-class types rather than free-form dicts because Evidence
First and constraint non-widening are invariants, and invariants need somewhere
to live.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qor.domain.enums import RiskLevel
from qor.kernel.errors import InvariantViolation

__all__ = ["Confidence", "Constraints", "EvidenceItem", "Uncertainty"]


class EvidenceItem(BaseModel):
    """One traceable piece of support for a claim.

    ``locator`` is what makes the claim checkable by someone else later; a claim
    whose evidence cannot be located again is not evidence, it is an assertion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    kind: str
    summary: str
    locator: str
    observed_at: datetime | None = None
    produced_by: str = ""

    @model_validator(mode="after")
    def _locator_present(self) -> Self:
        if not self.locator.strip():
            raise InvariantViolation("I9", "EvidenceItem.locator must be non-empty")
        return self


class Confidence(BaseModel):
    """A confidence score that must state its own basis.

    A bare number is unauditable: nobody can later ask "was 0.8 justified?".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float = Field(ge=0.0, le=1.0)
    basis: str
    sample_size: int | None = None

    @model_validator(mode="after")
    def _basis_present(self) -> Self:
        if not self.basis.strip():
            raise InvariantViolation("I9", "Confidence.basis must be non-empty")
        return self


class Uncertainty(BaseModel):
    """What the producer knows they do not know.

    Deliberately separate from Confidence: "I am 80% sure" and "here is what
    could invalidate this" are different statements, and the second one is what
    reviewers actually need.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    known_unknowns: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    invalidating_conditions: tuple[str, ...] = ()


class Constraints(BaseModel):
    """Bounds that may narrow down a task tree but never widen (invariant I22).

    ``None`` means "not constrained at this level"; a child may introduce a bound
    where the parent had none, but may never relax one the parent set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_risk_level: RiskLevel | None = None
    environments: frozenset[str] | None = None
    forbidden_capabilities: frozenset[str] = frozenset()
    max_cost_units: int | None = None
    deadline: datetime | None = None
    requires_dry_run: bool = False

    def narrow(self, child: Constraints) -> Constraints:
        """Return the intersection of self and child.

        Raises when the child tries to widen, rather than silently clamping: a
        plan that asks for more authority than its parent has is a defect the
        organization should see, not something to quietly fix.
        """
        if (
            child.max_risk_level is not None
            and self.max_risk_level is not None
            and child.max_risk_level > self.max_risk_level
        ):
            raise InvariantViolation(
                "I22",
                f"child max_risk_level {child.max_risk_level} exceeds parent {self.max_risk_level}",
            )
        if child.environments is not None and self.environments is not None:
            extra = child.environments - self.environments
            if extra:
                raise InvariantViolation(
                    "I22", f"child adds environments not permitted by parent: {sorted(extra)}"
                )
        if (
            child.max_cost_units is not None
            and self.max_cost_units is not None
            and child.max_cost_units > self.max_cost_units
        ):
            raise InvariantViolation(
                "I22",
                f"child max_cost_units {child.max_cost_units} exceeds parent {self.max_cost_units}",
            )
        if (
            child.deadline is not None
            and self.deadline is not None
            and child.deadline > self.deadline
        ):
            raise InvariantViolation(
                "I22", f"child deadline {child.deadline} is later than parent {self.deadline}"
            )
        if self.requires_dry_run and not child.requires_dry_run:
            raise InvariantViolation("I22", "child cannot drop parent's requires_dry_run")

        def tighter(a: Any, b: Any) -> Any:
            if a is None:
                return b
            if b is None:
                return a
            return min(a, b)

        return Constraints(
            max_risk_level=tighter(self.max_risk_level, child.max_risk_level),
            environments=(
                child.environments
                if self.environments is None
                else (
                    self.environments
                    if child.environments is None
                    else self.environments & child.environments
                )
            ),
            forbidden_capabilities=self.forbidden_capabilities | child.forbidden_capabilities,
            max_cost_units=tighter(self.max_cost_units, child.max_cost_units),
            deadline=tighter(self.deadline, child.deadline),
            requires_dry_run=self.requires_dry_run or child.requires_dry_run,
        )
