"""Planner port.

The signature is the architectural statement: ``PlanRequest`` hands the planner a
**capability catalog** and never an agent roster. A planner physically cannot name
an agent, because it has never been told that one exists. The most it can say is
which capability key the work requires.

Two implementations exist (Human CIO ruling E4):

* ``LLMPlanner``  -- proves an AI CIO can autonomously produce a legal DAG.
* ``StubPlanner`` -- deterministic, for CI. Test-only; it does not represent QOR's
  real planning capability.

The invariant that matters is that **delegation and lifecycle do not care which
one ran**. Any legal DAG must execute.
"""

from __future__ import annotations

from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from qor.domain.enums import ArtifactType, RiskLevel
from qor.domain.task import NodeKind
from qor.kernel.errors import InvariantViolation

__all__ = [
    "BINDING_ADDRESS_CONTRADICTED",
    "BINDING_ADDRESS_FULFILLED_RESPONSE",
    "BindingConstraint",
    "LessonFact",
    "NodeDraft",
    "OutcomeFact",
    "PlanDraft",
    "PlanRequest",
    "PlannerPort",
    "binding_constraint_violations",
]

BINDING_ADDRESS_CONTRADICTED = "ADDRESS_CONTRADICTED_OUTCOME"
BINDING_ADDRESS_FULFILLED_RESPONSE = "ADDRESS_FULFILLED_RESPONSE"


class CapabilityOffer(BaseModel):
    """One capability the organization can perform, as shown to the planner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    name: str
    description: str
    risk_class: str


class OutcomeFact(BaseModel):
    """A prior Outcome the planner is allowed to see. No agent fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    decision_ref: str
    subject_key: str
    verdict: str
    contradicted_decision_type: str
    confidence: str
    status: str
    statement: str = ""
    follow_through_status: str = ""
    fulfilling_decision_ref: str = ""


class LessonFact(BaseModel):
    """A prior APPROVED Lesson the planner may see as context. Not a constraint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    applicability_subject_key: str
    statement: str = ""
    source_outcome_refs: tuple[str, ...] = ()


class BindingConstraint(BaseModel):
    """A structured obligation the resulting plan must satisfy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    outcome_ref: str
    subject_key: str
    contradicted_decision_type: str
    decision_ref: str = ""


class PlanRequest(BaseModel):
    """Everything a planner is allowed to know.

    Deliberately absent: agent names, agent counts, agent availability. Adding any
    of them would let the planner schedule instead of plan.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_ref: str
    title: str
    objective: str
    risk_level: RiskLevel
    required_outputs: tuple[ArtifactType, ...] = ()
    available_capabilities: tuple[CapabilityOffer, ...] = ()
    max_nodes: int = 10
    # Populated on replan so the planner can respond to what went wrong.
    previous_failure_feedback: tuple[str, ...] = ()
    # Organization facts about prior Decisions. Visible to the planner; only
    # ``binding_constraints`` are mandatory to satisfy. Never includes agents.
    prior_outcomes: tuple[OutcomeFact, ...] = ()
    prior_lessons: tuple[LessonFact, ...] = ()
    binding_constraints: tuple[BindingConstraint, ...] = ()

    @model_validator(mode="after")
    def _no_agent_leakage(self) -> Self:
        banned = ("agent_", "agents", "roster", "assignee")
        for field in type(self).model_fields:
            if any(token in field.lower() for token in banned):
                raise InvariantViolation(
                    "I2", f"PlanRequest must not expose agents; found field '{field}'"
                )
        return self


class NodeDraft(BaseModel):
    """A planner's proposal for one node.

    ``key`` is planner-local (e.g. ``"n1"``) and only used to express edges. Real
    UUIDs are minted by the planning service, so a planner cannot forge an id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    name: str
    objective: str
    required_capabilities: tuple[str, ...]
    expected_artifact_type: ArtifactType = ArtifactType.ANALYSIS_REPORT
    node_kind: str = NodeKind.WORK
    depends_on: tuple[str, ...] = ()
    review_required: bool = True
    # Structured response to a binding constraint. Empty means "does not address
    # any Outcome". A rationale that quotes an Outcome is not a substitute.
    addresses_outcome_ref: str = ""
    addresses_decision_ref: str = ""

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        if not self.key.strip():
            raise InvariantViolation("I2", "NodeDraft.key must be non-empty")
        if not self.required_capabilities:
            raise InvariantViolation(
                "I2", f"NodeDraft '{self.key}' declares no required_capabilities"
            )
        if self.node_kind not in NodeKind.ALL:
            raise InvariantViolation(
                "I2", f"NodeDraft '{self.key}' has invalid node_kind '{self.node_kind}'"
            )
        return self


class PlanDraft(BaseModel):
    """A planner's full proposal. Still untrusted at this point.

    The planning service validates it (DAG shape, capability existence, output
    coverage) before anything is persisted. A planner producing nonsense results in
    a blocked task with a recorded reason, not a crash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[NodeDraft, ...]
    decision_rationale: str
    expected_outputs: tuple[ArtifactType, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if not self.nodes:
            raise InvariantViolation("I-P3", "PlanDraft has no nodes")
        if not self.decision_rationale.strip():
            # Audit question 2 is "why did the AI CIO split the task this way".
            # An unexplained plan cannot answer it.
            raise InvariantViolation("I-P5", "PlanDraft.decision_rationale must be non-empty")
        keys = [n.key for n in self.nodes]
        if len(set(keys)) != len(keys):
            raise InvariantViolation("I-P2", f"PlanDraft has duplicate node keys: {keys}")
        known = set(keys)
        for node in self.nodes:
            unknown = [d for d in node.depends_on if d not in known]
            if unknown:
                raise InvariantViolation(
                    "I-P2", f"node '{node.key}' depends on unknown keys {unknown}"
                )
            if node.key in node.depends_on:
                raise InvariantViolation("I-P1", f"node '{node.key}' depends on itself")

        # A longer cycle is caught here rather than later at TaskPlan construction,
        # because a planner that produced one can be told what was wrong and try
        # again. Detecting it downstream would surface it as an unrelated failure
        # after the repair opportunity has passed.
        pending = {n.key: set(n.depends_on) for n in self.nodes}
        while True:
            ready = [key for key, deps in pending.items() if not deps]
            if not ready:
                break
            for key in ready:
                del pending[key]
            for deps in pending.values():
                deps.difference_update(ready)
        if pending:
            raise InvariantViolation(
                "I-P1", f"plan contains a dependency cycle among {sorted(pending)}"
            )
        return self


def binding_constraint_violations(
    nodes: tuple[NodeDraft, ...],
    constraints: tuple[BindingConstraint, ...],
) -> tuple[str, ...]:
    """Return one message per unsatisfied constraint.

    Satisfaction is structural. Rationale text is not inspected. Outcome constraints
    require ``addresses_outcome_ref``; fulfilled-response constraints require
    ``addresses_decision_ref``.
    """
    by_key = {node.key: node for node in nodes}
    recs = [n for n in nodes if n.expected_artifact_type is ArtifactType.RECOMMENDATION]
    messages: list[str] = []
    for constraint in constraints:
        if constraint.kind == BINDING_ADDRESS_FULFILLED_RESPONSE:
            addressing = [
                n.key for n in nodes if n.addresses_decision_ref == constraint.decision_ref
            ]
            if not addressing:
                messages.append(
                    f"plan does not acknowledge fulfilled response {constraint.decision_ref}: "
                    "no node declares addresses_decision_ref for it"
                )
            continue
        addressing = [n.key for n in nodes if n.addresses_outcome_ref == constraint.outcome_ref]
        if not addressing:
            messages.append(
                f"plan does not address contradicted outcome {constraint.outcome_ref}: "
                "no node declares addresses_outcome_ref for it"
            )
            continue
        if not recs:
            messages.append(
                f"plan does not address contradicted outcome {constraint.outcome_ref}: "
                "no RECOMMENDATION node exists to depend on the addressing node"
            )
            continue
        covered = False
        for rec in recs:
            upstream: set[str] = set()
            pending = list(rec.depends_on)
            while pending:
                key = pending.pop()
                if key in upstream or key not in by_key:
                    continue
                upstream.add(key)
                pending.extend(by_key[key].depends_on)
            if any(key in upstream for key in addressing):
                covered = True
                break
        if not covered:
            messages.append(
                f"plan does not address contradicted outcome {constraint.outcome_ref}: "
                "RECOMMENDATION does not depend on the addressing node"
            )
    return tuple(messages)


class PlannerPort(Protocol):
    """Turns an objective into a legal plan. Knows nothing about agents."""

    kind: str

    def plan(self, request: PlanRequest) -> PlanDraft: ...
