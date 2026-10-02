"""Organization aggregate: Organization, Department, Role, Capability, Agent.

The central design commitment here is Agent != Role. An Agent is a runtime
resource; a Role is an authority template. The same agent holding two roles has
the union of their permissions, and audit records which role it acted under.

Humans and AI agents are both Agent rows, distinguished by ``kind``. That is what
lets delegation, review and approval share one code path.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qor.domain.base import Entity
from qor.domain.enums import OperatingMode, RiskClass, RiskLevel
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import InvariantViolation

__all__ = [
    "Agent",
    "AgentCapability",
    "Capability",
    "Department",
    "Organization",
    "Role",
]


class Organization(Entity):
    """The company itself. Holds the charter and the global kill switch."""

    ENTITY_TYPE: ClassVar[str] = "Organization"
    REF_PREFIX: ClassVar[str] = "ORG"

    name: str
    mission: str
    operating_mode: OperatingMode = OperatingMode.NORMAL
    charter_version: str = "1.0"
    # Absolute ceilings. No task, plan or decision may exceed these.
    hard_limits: dict[str, float] = Field(default_factory=dict)
    human_cio_agent_id: UUID | None = None

    @property
    def is_halted(self) -> bool:
        return self.operating_mode is OperatingMode.HALTED


class Department(Entity):
    """A capability domain with accountability, not an org chart box."""

    ENTITY_TYPE: ClassVar[str] = "Department"
    REF_PREFIX: ClassVar[str] = "DEPT"

    organization_id: UUID
    name: str
    mandate: str
    parent_department_id: UUID | None = None
    owns_capabilities: tuple[str, ...] = ()
    accountable_for: tuple[str, ...] = ()


class Role(Entity):
    """An authority template.

    ``granted_permissions`` is the only place authority originates. Agents never
    hold intrinsic permissions -- see Agent.effective_permissions.
    """

    ENTITY_TYPE: ClassVar[str] = "Role"
    REF_PREFIX: ClassVar[str] = "ROLE"

    organization_id: UUID
    department_id: UUID | None = None
    name: str
    purpose: str
    granted_permissions: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()
    max_risk_level: RiskLevel = RiskLevel.ROUTINE
    can_approve_up_to: RiskLevel | None = None
    human_only: bool = False


class Capability(Entity):
    """What kind of work can be done, independent of who does it.

    ``risk_class`` is the mechanism that routes work to the Action Gateway.
    Note it is deliberately hard to change: see invariant I26.
    """

    ENTITY_TYPE: ClassVar[str] = "Capability"
    REF_PREFIX: ClassVar[str] = "CAP"

    key: str
    name: str
    description: str
    risk_class: RiskClass = RiskClass.READ_ONLY
    required_permissions: frozenset[str] = frozenset()
    # Reviews this capability's output always needs, regardless of task risk.
    mandatory_review_types: tuple[str, ...] = ()
    verifiable: bool = True

    @model_validator(mode="after")
    def _key_is_namespaced(self) -> Self:
        if "." not in self.key:
            raise InvariantViolation(
                "I2",
                f"capability key '{self.key}' must be namespaced, e.g. 'work.record'",
            )
        return self


class AgentCapability(BaseModel):
    """An agent's grip on one capability.

    ``proficiency`` is SYSTEM-only (see §5.21.3): if an agent could raise its own
    proficiency it could win delegations it should not.
    """

    model_config = ConfigDict(extra="forbid")

    capability_key: str
    proficiency: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_count: int = 0
    last_used_at: datetime | None = None
    certified_by: UUID | None = None


class Agent(Entity):
    """A runtime resource that can hold assignments.

    ``kind=HUMAN`` rows exist so that the Human CIO is a first-class participant
    with an audit trail, not a special case outside the model.
    """

    ENTITY_TYPE: ClassVar[str] = "Agent"
    REF_PREFIX: ClassVar[str] = "AGT"

    organization_id: UUID
    kind: ActorKind
    name: str
    role_ids: tuple[UUID, ...] = ()
    capabilities: tuple[AgentCapability, ...] = ()
    # Explicit subtractions applied after the role union. Denial always wins.
    denied_permissions: frozenset[str] = frozenset()
    max_concurrent_assignments: int = 1
    current_assignment_count: int = 0
    trust_score: float = Field(default=0.5, ge=0.0, le=1.0)
    heartbeat_at: datetime | None = None
    runtime_handle: str = ""

    @model_validator(mode="after")
    def _kind_is_principal(self) -> Self:
        if self.kind in (ActorKind.SYSTEM, ActorKind.RULE):
            raise InvariantViolation(
                "I3",
                f"Agent.kind cannot be {self.kind}: SYSTEM/RULE are not accountable principals",
            )
        return self

    # ---- queries -------------------------------------------------------

    @property
    def is_ai(self) -> bool:
        return self.kind is ActorKind.AGENT

    @property
    def has_capacity(self) -> bool:
        return self.current_assignment_count < self.max_concurrent_assignments

    def capability(self, key: str) -> AgentCapability | None:
        for cap in self.capabilities:
            if cap.capability_key == key:
                return cap
        return None

    def holds(self, key: str) -> bool:
        return self.capability(key) is not None

    def effective_permissions(self, roles: dict[UUID, Role]) -> frozenset[str]:
        """Layer 1 and 2 of the three-layer narrowing:

            union(roles.granted) - agent.denied

        Layer 3 (assignment scope) is applied by the authorization service, since
        it depends on the specific assignment.
        """
        granted: set[str] = set()
        for role_id in self.role_ids:
            role = roles.get(role_id)
            if role is not None:
                granted |= set(role.granted_permissions)
        return frozenset(granted - self.denied_permissions)

    def as_actor(self, role_id: UUID | None = None) -> ActorRef:
        return ActorRef(
            kind=self.kind,
            id=self.id,
            role_id=role_id or (self.role_ids[0] if self.role_ids else None),
            display=self.name,
        )
