"""Decision -- a commitment, deliberately powerless.

Decision has no ``payload`` and no execution parameters. That absence is the
point (invariant I10): a Decision says what the organization commits to, an Action
says which operation would carry that commitment out. Keeping them
apart is what lets the organization approve intent and gate execution separately.

``alternatives_considered`` and ``measurement_plan`` are required because decision
quality must be judgeable independently of outcome. Without them, a lucky bad
decision is indistinguishable from a good one.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from qor.domain.base import Entity
from qor.domain.enums import DecisionType, RiskLevel
from qor.domain.values import Confidence
from qor.kernel.errors import InvariantViolation

__all__ = [
    "Alternative",
    "Decision",
    "MeasurementEvaluation",
    "MeasurementPlan",
    "SuccessCheck",
]

SUCCESS_CHECK_OPERATORS = frozenset({"EQ", "GE", "LE", "GT", "LT"})
SUCCESS_CHECK_SOURCES = frozenset({"WORLD_LEDGER", "ACTION_VERIFICATION_OBSERVED"})
EVALUATE_WHEN_VALUES = frozenset({"AFTER_SETTLEMENT", "HUMAN_REQUESTED"})
MEASUREMENT_EVALUATION_VERDICTS = frozenset({"SUCCESS", "FAILED", "UNCERTAIN"})
LEDGER_OBSERVABLE_METRICS = frozenset({"sandbox.value", "value"})


class Alternative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    option: str
    pros: tuple[str, ...] = ()
    cons: tuple[str, ...] = ()
    rejected_because: str = ""


class SuccessCheck(BaseModel):
    """Decision-local executable clause. Not a global metric catalog entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    operator: str
    threshold: str
    observation_source: str

    @model_validator(mode="after")
    def _closed_contract(self) -> Self:
        if not self.metric.strip():
            raise InvariantViolation("I-D3", "SuccessCheck.metric must be non-empty")
        if not self.threshold.strip():
            raise InvariantViolation("I-D3", "SuccessCheck.threshold must be non-empty")
        if self.operator not in SUCCESS_CHECK_OPERATORS:
            raise InvariantViolation(
                "I-D3",
                f"SuccessCheck.operator '{self.operator}' is not one of "
                f"{sorted(SUCCESS_CHECK_OPERATORS)}",
            )
        if self.observation_source not in SUCCESS_CHECK_SOURCES:
            raise InvariantViolation(
                "I-D3",
                f"SuccessCheck.observation_source '{self.observation_source}' is not one of "
                f"{sorted(SUCCESS_CHECK_SOURCES)}",
            )
        return self


class MeasurementEvaluation(BaseModel):
    """Structured result of checking a frozen Decision measurement contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str
    observed: dict[str, str]
    criteria_applied: tuple[SuccessCheck, ...] = ()
    observation_source: str = ""
    eligibility: str
    evaluated_at: datetime
    action_ref: str = ""

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if self.verdict not in MEASUREMENT_EVALUATION_VERDICTS:
            raise InvariantViolation(
                "I-D5",
                f"MeasurementEvaluation.verdict '{self.verdict}' is not one of "
                f"{sorted(MEASUREMENT_EVALUATION_VERDICTS)}",
            )
        if self.eligibility not in EVALUATE_WHEN_VALUES:
            raise InvariantViolation(
                "I-D5",
                f"MeasurementEvaluation.eligibility '{self.eligibility}' is not one of "
                f"{sorted(EVALUATE_WHEN_VALUES)}",
            )
        return self


class MeasurementPlan(BaseModel):
    """How we will know later whether this decision was right.

    Recorded at decision time on purpose: choosing the yardstick after seeing the
    result is how organizations fool themselves. ``success_criteria`` remains
    human-readable text. Only ``success_checks`` are runtime-evaluable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: tuple[str, ...] = ()
    evaluate_after: str = ""
    success_criteria: tuple[str, ...] = ()
    success_checks: tuple[SuccessCheck, ...] = ()
    evaluate_when: str = ""

    @model_validator(mode="after")
    def _has_metric(self) -> Self:
        if not self.metrics:
            raise InvariantViolation("I-D3", "MeasurementPlan must name at least one metric")
        if self.evaluate_when and self.evaluate_when not in EVALUATE_WHEN_VALUES:
            raise InvariantViolation(
                "I-D3",
                f"MeasurementPlan.evaluate_when '{self.evaluate_when}' is not one of "
                f"{sorted(EVALUATE_WHEN_VALUES)}",
            )
        allowed = set(self.metrics)
        unknown = [check.metric for check in self.success_checks if check.metric not in allowed]
        if unknown:
            raise InvariantViolation(
                "I-D3",
                f"SuccessCheck metrics not on the measurement plan: {unknown}",
            )
        return self


class Decision(Entity):
    ENTITY_TYPE: ClassVar[str] = "Decision"
    REF_PREFIX: ClassVar[str] = "DEC"

    task_id: UUID
    decision_type: DecisionType
    statement: str
    rationale: str

    decided_by_agent_id: UUID | None = None
    decided_by_role_id: UUID | None = None
    decided_at: datetime | None = None

    # Evidence chain: which artifacts and reviews justify this.
    based_on_artifact_ids: tuple[UUID, ...] = ()
    based_on_review_ids: tuple[UUID, ...] = ()

    alternatives_considered: tuple[Alternative, ...] = ()
    measurement_plan: MeasurementPlan | None = None
    confidence: Confidence | None = None
    risk_level: RiskLevel = RiskLevel.ROUTINE
    reversibility: str = "REVERSIBLE"

    requires_approval: bool = False
    approval_policy_ref: str = ""
    supersedes_decision_id: UUID | None = None
    expires_at: datetime | None = None
    addresses_outcome_ref: str = ""
    measurement_evaluations: tuple[MeasurementEvaluation, ...] = ()

    @model_validator(mode="after")
    def _no_execution_details(self) -> Self:
        if not self.statement.strip():
            raise InvariantViolation("I10", "Decision.statement must be non-empty")
        if not self.rationale.strip():
            raise InvariantViolation("I10", "Decision.rationale must be non-empty")
        return self

    @property
    def requires_no_action(self) -> bool:
        return self.decision_type in (DecisionType.NO_ACTION, DecisionType.RECOMMEND)
