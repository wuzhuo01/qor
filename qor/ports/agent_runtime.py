"""Agent runtime port.

QOR asks "perform this node, which requires these capabilities" and gets back
structured findings plus evidence. It never asks "run agent X".

The request carries ``required_capabilities`` rather than an agent identity for the
same reason the planner never sees agents: the runtime is a resource pool, and the
organization addresses it by what it needs, not by who it knows.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from qor.domain.artifact import Artifact
from qor.domain.decision import SuccessCheck
from qor.domain.enums import ArtifactType, FindingSeverity
from qor.domain.values import Confidence, EvidenceItem, Uncertainty

__all__ = [
    "ActionProposal",
    "AgentRuntimePort",
    "DecisionProposal",
    "DecisionRequest",
    "DraftFinding",
    "OpinionFinding",
    "ReviewOpinion",
    "ReviewRequest",
    "WorkRequest",
    "WorkResult",
]


class WorkRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ref: str
    task_objective: str
    node_ref: str
    node_name: str
    node_objective: str
    required_capabilities: tuple[str, ...]
    expected_artifact_type: ArtifactType
    input_artifacts: tuple[Artifact, ...] = ()
    attempt: int = 1
    # Non-empty on rework: the reviewer's required_changes from the previous round.
    required_changes: tuple[str, ...] = ()
    scope: frozenset[str] = frozenset()


class DraftFinding(BaseModel):
    """A claim plus the locators of the evidence supporting it.

    Locators rather than ids: the runtime does not mint EvidenceItem ids, so it
    refers to its own evidence by locator and the execution service resolves them.
    This keeps id generation inside QOR.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str
    evidence_locators: tuple[str, ...]
    confidence: Confidence | None = None


class WorkResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    content: str
    findings: tuple[DraftFinding, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    confidence: Confidence | None = None
    uncertainty: Uncertainty = Field(default_factory=Uncertainty)
    methodology: str = ""
    data_sources: tuple[str, ...] = ()
    cost_units: int = 1


class ReviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ref: str
    review_ref: str
    review_type: str
    round: int
    artifact: Artifact
    rubric: tuple[str, ...] = ()
    # Set on round >= 2 so the reviewer can check the previous complaints were met.
    previous_required_changes: tuple[str, ...] = ()
    node_objective: str = ""
    node_name: str = ""


class OpinionFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: FindingSeverity
    statement: str
    location: str = ""
    suggested_fix: str = ""


class ReviewOpinion(BaseModel):
    """A reviewer's judgement. QOR, not the reviewer, applies it to state.

    The reviewer proposes ``verdict``; the review service still runs the state
    machine guards, so a reviewer claiming PASSED while reporting a BLOCKER finding
    is rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: str  # PASSED | FAILED | CONDITIONAL
    summary: str
    findings: tuple[OpinionFinding, ...] = ()
    required_changes: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()


class ActionProposal(BaseModel):
    """A concrete operation the decision would require.

    Note it carries no rationale. The rationale lives on the Decision; an action
    that could justify itself could authorize itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    resource: str
    environment: str
    operation: str
    capability_key: str
    payload: dict[str, object] = Field(default_factory=dict)
    is_reversible: bool = True
    dry_run: bool = True


class DecisionProposal(BaseModel):
    """What the AI CIO concluded, in a shape the Decision guards can check.

    Alternatives and a measurement plan are required by the domain, so they are
    required here too: an agent that cannot say what it rejected and how it would
    be measured cannot get a decision past ``propose``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_type: str
    statement: str
    rationale: str
    alternatives: tuple[dict[str, object], ...] = ()
    metrics: tuple[str, ...] = ()
    evaluate_after: str = ""
    success_criteria: tuple[str, ...] = ()
    success_checks: tuple[SuccessCheck, ...] = ()
    evaluate_when: str = ""
    confidence: Confidence | None = None
    risk_level: str = "ROUTINE"
    is_reversible: bool = True
    proposed_actions: tuple[ActionProposal, ...] = ()
    addresses_outcome_ref: str = ""


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ref: str
    task_objective: str
    accepted_artifacts: tuple[Artifact, ...] = ()
    risk_level: str = "ROUTINE"
    # The default environment is SANDBOX. The runtime must not propose live actions.
    permitted_environments: tuple[str, ...] = ("SANDBOX",)
    addresses_outcome_ref: str = ""


class AgentRuntimePort(Protocol):
    """Executes work, reviews and decision proposals. Addressed by capability."""

    def execute_work(self, agent_id: UUID, request: WorkRequest) -> WorkResult: ...

    def execute_review(self, agent_id: UUID, request: ReviewRequest) -> ReviewOpinion: ...

    def propose_decision(self, agent_id: UUID, request: DecisionRequest) -> DecisionProposal: ...
