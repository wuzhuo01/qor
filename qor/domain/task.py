"""Task aggregate: Task, TaskPlan, TaskNode, TaskEdge, TaskAssignment.

The load-bearing detail in this module: **TaskNode has no agent field.**

A plan describes what must be true, in terms of required capabilities. Who does
it is decided later, by delegation, and recorded on TaskAssignment. If TaskNode
carried an ``agent_id`` then planning would be scheduling, and "Task First, Agent
Second" would be a slogan instead of a constraint. ``tests/test_architecture.py``
fails the build if an agent field ever appears here.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qor.domain.base import Entity
from qor.domain.enums import ArtifactType, RiskLevel, TaskOrigin
from qor.domain.values import Constraints
from qor.kernel.errors import InvariantViolation

__all__ = [
    "AgentSelection",
    "NodeKind",
    "ReviewPolicy",
    "Task",
    "TaskAssignment",
    "TaskEdge",
    "TaskNode",
    "TaskPlan",
]


class NodeKind:
    """Whether a node produces primary evidence or consumes other nodes' output.

    This is not a workflow phase label and carries no agent information. It exists
    because the Task machine separates RUNNING (evidence production) from
    SYNTHESIZING (integration), and a guard needs to tell the two apart.
    """

    WORK = "WORK"
    SYNTHESIS = "SYNTHESIS"

    ALL: ClassVar[tuple[str, ...]] = (WORK, SYNTHESIS)


class ReviewPolicy(BaseModel):
    """Whether and how this node's output must be independently checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required: bool = True
    review_types: tuple[str, ...] = ("METHODOLOGY",)
    min_reviewers: int = 1
    reviewer_capability: str = "governance.review"


class Task(Entity):
    """The only entry point into the organization (invariant I1).

    Nothing gets worked on unless it is a Task -- not a chat message, not a
    scheduled script, not an agent's own idea. Those all become Tasks first.
    """

    ENTITY_TYPE: ClassVar[str] = "Task"
    REF_PREFIX: ClassVar[str] = "TASK"

    organization_id: UUID
    title: str
    objective: str
    origin: TaskOrigin
    requested_by: UUID | None = None
    owner_agent_id: UUID | None = None
    sponsor_agent_id: UUID | None = None
    parent_task_id: UUID | None = None
    source_outcome_id: UUID | None = None
    risk_level: RiskLevel = RiskLevel.ROUTINE
    constraints: Constraints = Field(default_factory=Constraints)
    required_outputs: tuple[ArtifactType, ...] = ()
    active_plan_id: UUID | None = None
    plan_revision_count: int = 0
    max_plan_revisions: int = 2
    completion_rationale: str = ""
    deadline: datetime | None = None

    @model_validator(mode="after")
    def _objective_present(self) -> Self:
        if not self.objective.strip():
            raise InvariantViolation("I1", "Task.objective must be non-empty")
        return self


class TaskEdge(BaseModel):
    """A dependency between two nodes. Authoritative source of graph shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_node_id: UUID
    to_node_id: UUID
    reason: str = ""


class TaskPlan(BaseModel):
    """How a Task will be pursued. Immutable once ACTIVE.

    Not an Entity subclass only because it is the aggregate root's child and is
    versioned rather than mutated; it still carries id/ref/status/version and is
    driven by the same state machine engine.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    ENTITY_TYPE: ClassVar[str] = "TaskPlan"
    REF_PREFIX: ClassVar[str] = "PLAN"

    id: UUID
    ref: str
    status: str
    version: int = 1
    task_id: UUID
    revision: int = 1
    created_at: datetime
    created_by: object  # ActorRef; typed loosely to avoid a circular import cost
    updated_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)

    node_ids: tuple[UUID, ...] = ()
    edges: tuple[TaskEdge, ...] = ()
    strategy_rationale: str = ""
    planner_kind: str = ""
    planner_provider: str = ""
    supersedes_plan_id: UUID | None = None
    expected_outputs: tuple[ArtifactType, ...] = ()

    @property
    def entity_type(self) -> str:
        return self.ENTITY_TYPE

    def upstream_of(self, node_id: UUID) -> tuple[UUID, ...]:
        return tuple(e.from_node_id for e in self.edges if e.to_node_id == node_id)

    def downstream_of(self, node_id: UUID) -> tuple[UUID, ...]:
        return tuple(e.to_node_id for e in self.edges if e.from_node_id == node_id)

    def entry_nodes(self) -> tuple[UUID, ...]:
        has_upstream = {e.to_node_id for e in self.edges}
        return tuple(n for n in self.node_ids if n not in has_upstream)

    def exit_nodes(self) -> tuple[UUID, ...]:
        has_downstream = {e.from_node_id for e in self.edges}
        return tuple(n for n in self.node_ids if n not in has_downstream)

    def topological_order(self) -> tuple[UUID, ...]:
        """Kahn's algorithm. Raises if the graph is not a DAG (invariant I-P1)."""
        indegree = {n: 0 for n in self.node_ids}
        for edge in self.edges:
            if edge.to_node_id not in indegree or edge.from_node_id not in indegree:
                raise InvariantViolation(
                    "I-P2", f"edge {edge.from_node_id}->{edge.to_node_id} references unknown node"
                )
            indegree[edge.to_node_id] += 1

        # Preserve declared node order among ready nodes so runs are reproducible.
        ready = [n for n in self.node_ids if indegree[n] == 0]
        order: list[UUID] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for nxt in self.downstream_of(node):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort(key=lambda n: self.node_ids.index(n))
        if len(order) != len(self.node_ids):
            cyclic = sorted(str(n) for n in self.node_ids if n not in order)
            raise InvariantViolation("I-P1", f"plan is not a DAG; nodes in cycle: {cyclic}")
        return tuple(order)

    @property
    def is_dag(self) -> bool:
        try:
            self.topological_order()
        except InvariantViolation:
            return False
        return True


class TaskNode(Entity):
    """One unit of work, described by required capability -- never by agent.

    Do not add an ``agent_id``, ``assignee`` or ``executor`` field here. The
    architecture test enforces this and the reason is in the module docstring.
    """

    ENTITY_TYPE: ClassVar[str] = "TaskNode"
    REF_PREFIX: ClassVar[str] = "NODE"

    task_id: UUID
    plan_id: UUID
    name: str
    objective: str
    node_kind: str = NodeKind.WORK

    # The only basis for delegation (invariant I2).
    required_capabilities: tuple[str, ...] = ()
    expected_artifact_type: ArtifactType = ArtifactType.ANALYSIS_REPORT
    input_artifact_ids: tuple[UUID, ...] = ()
    output_artifact_ids: tuple[UUID, ...] = ()

    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    attempt: int = 1
    max_attempts: int = 2
    constraints: Constraints = Field(default_factory=Constraints)
    skip_condition: str = ""
    addresses_outcome_ref: str = ""
    addresses_decision_ref: str = ""

    # SYSTEM-only. Populated by the review service to keep producer != reviewer,
    # and by delegation to exclude agents that already failed this node.
    forbidden_agent_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def _requires_capability(self) -> Self:
        if not self.required_capabilities:
            raise InvariantViolation(
                "I2", f"TaskNode '{self.name}' declares no required_capabilities"
            )
        if self.node_kind not in NodeKind.ALL:
            raise InvariantViolation(
                "I2", f"TaskNode.node_kind '{self.node_kind}' is not one of {NodeKind.ALL}"
            )
        return self

    @property
    def attempts_available(self) -> bool:
        return self.attempt < self.max_attempts


class AgentSelection(BaseModel):
    """Why this agent, and why not the others.

    SYSTEM-only (§5.21.3). This is what answers audit question 4, "why was this
    agent chosen", without anyone having to reconstruct it from logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_capabilities: tuple[str, ...] = ()
    candidates_considered: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()  # (agent ref, reason)
    selected_agent_ref: str = ""
    score: float = 0.0
    rationale: str = ""


class TaskAssignment(Entity):
    """The binding of one node to one agent, for one attempt.

    Carries its own permission grant so that authority is scoped to the work:
    an agent's role may allow more than this particular assignment needs, and the
    narrower of the two wins (§5.21.1 layer 3).
    """

    ENTITY_TYPE: ClassVar[str] = "TaskAssignment"
    REF_PREFIX: ClassVar[str] = "ASGN"

    task_id: UUID
    node_id: UUID
    assignee_agent_id: UUID
    assignee_role_id: UUID | None = None
    attempt: int = 1

    granted_permissions: frozenset[str] = frozenset()
    scope: frozenset[str] = frozenset()
    constraints: Constraints = Field(default_factory=Constraints)
    selection: AgentSelection = Field(default_factory=AgentSelection)

    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_artifact_ids: tuple[UUID, ...] = ()
    cost_units: int = 0
