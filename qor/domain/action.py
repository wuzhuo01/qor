"""Action -- the only exit to reality (invariant I9/Action Gateway).

Action has no ``rationale`` field, mirroring Decision having no ``payload``. An
Action that could justify itself would be an Action that could authorize itself.
It points at a ``decision_id`` instead, and if that link is missing the gateway
refuses it.

Dry run is not a state. ``environment=SANDBOX`` plus ``dry_run=True`` reuses the
same lifecycle with a different policy stop state, and the ``action_is_not_dry_run``
guard on the ``execute`` transition means a dry run physically cannot reach
EXECUTING.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qor.domain.base import Entity
from qor.domain.enums import Environment
from qor.kernel.errors import InvariantViolation

__all__ = ["Action", "ActionCheck", "ActionTarget", "Verification"]


class ActionTarget(BaseModel):
    """What system, and in which environment, this action would touch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    resource: str
    environment: Environment
    operation: str


class ActionCheck(BaseModel):
    """One gate the action passed or failed, with the policy that decided it.

    ``WAIVED`` is a first-class verdict: in V0.1 a SANDBOX dry run does
    not need human approval, and recording "waived by policy X" is honest, whereas
    skipping the check entirely would leave a hole in the audit chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str  # PERMISSION | RISK | APPROVAL
    verdict: str  # PASSED | FAILED | WAIVED
    policy_ref: str = ""
    detail: str = ""
    checked_at: datetime | None = None

    @model_validator(mode="after")
    def _verdict_valid(self) -> Self:
        if self.verdict not in ("PASSED", "FAILED", "WAIVED"):
            raise InvariantViolation(
                "I-AC2", f"ActionCheck.verdict '{self.verdict}' must be PASSED/FAILED/WAIVED"
            )
        if self.verdict == "WAIVED" and not self.policy_ref:
            raise InvariantViolation("I-AC2", "a WAIVED check must name the policy that waived it")
        return self


class Verification(BaseModel):
    """Read-back after execution. EXECUTED is not the same as it worked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: dict[str, str] = Field(default_factory=dict)
    observed: dict[str, str] = Field(default_factory=dict)
    matched: bool = False
    verified_at: datetime | None = None


class Action(Entity):
    ENTITY_TYPE: ClassVar[str] = "Action"
    REF_PREFIX: ClassVar[str] = "ACT"

    task_id: UUID
    decision_id: UUID
    requested_by_agent_id: UUID
    requested_by_role_id: UUID | None = None

    target: ActionTarget
    payload: dict[str, object] = Field(default_factory=dict)
    capability_key: str = ""

    dry_run: bool = True
    dry_run_result: dict[str, object] = Field(default_factory=dict)
    is_reversible: bool = True
    idempotency_key: str

    checks: tuple[ActionCheck, ...] = ()
    verification: Verification | None = None
    execution_attempts: int = 0
    max_execution_attempts: int = 1

    @model_validator(mode="after")
    def _must_cite_a_decision(self) -> Self:
        """Invariant I10: no action without a decision that authorized it."""
        if not self.idempotency_key.strip():
            raise InvariantViolation("I-AC1", "Action.idempotency_key must be non-empty")
        if self.dry_run and self.target.environment is Environment.LIVE:
            raise InvariantViolation("I-AC3", "a dry_run action cannot target the LIVE environment")
        return self

    # ---- queries -------------------------------------------------------

    def check(self, kind: str) -> ActionCheck | None:
        for item in self.checks:
            if item.kind == kind:
                return item
        return None

    @property
    def all_checks_passed(self) -> bool:
        required = ("PERMISSION", "RISK", "APPROVAL")
        for kind in required:
            found = self.check(kind)
            if found is None or found.verdict == "FAILED":
                return False
        return True

    @property
    def stop_state_key(self) -> str:
        """Which stop_states entry in action.yaml applies to this action."""
        return "dry_run" if self.dry_run else "live"
