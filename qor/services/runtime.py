"""Runtime container.

Holds the repositories, the append-only log, the clock, the state machine registry
and the policies. Services take a Runtime rather than a dozen constructor
arguments, which keeps wiring in one place and makes it obvious that everything
shares one log and one clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from qor.adapters.persistence.event_routes import EventRouteRepository
from qor.adapters.persistence.inmemory import InMemoryLogStore, InMemoryRepository
from qor.adapters.persistence.notifications import NotificationIntentRepository
from qor.adapters.persistence.responsibilities import ResponsibilityRepository
from qor.domain.action import Action
from qor.domain.artifact import Artifact
from qor.domain.decision import Decision
from qor.domain.lesson import Lesson
from qor.domain.organization import Agent, Capability, Department, Organization, Role
from qor.domain.outcome import Outcome
from qor.domain.review import Review
from qor.domain.schedule import Schedule
from qor.domain.task import Task, TaskAssignment, TaskNode, TaskPlan
from qor.kernel.clock import Clock, SystemClock
from qor.kernel.ids import RefMinter, new_id
from qor.kernel.statemachine import StateMachineRegistry
from qor.services.config import Policies, QORConfig

__all__ = ["Repos", "Runtime"]


@dataclass
class Repos:
    organizations: InMemoryRepository[Organization]
    departments: InMemoryRepository[Department]
    roles: InMemoryRepository[Role]
    capabilities: InMemoryRepository[Capability]
    agents: InMemoryRepository[Agent]
    tasks: InMemoryRepository[Task]
    plans: InMemoryRepository[TaskPlan]
    nodes: InMemoryRepository[TaskNode]
    assignments: InMemoryRepository[TaskAssignment]
    artifacts: InMemoryRepository[Artifact]
    reviews: InMemoryRepository[Review]
    decisions: InMemoryRepository[Decision]
    actions: InMemoryRepository[Action]
    outcomes: InMemoryRepository[Outcome]
    lessons: InMemoryRepository[Lesson]
    schedules: InMemoryRepository[Schedule]
    responsibilities: ResponsibilityRepository

    @classmethod
    def build(cls, log: InMemoryLogStore) -> Repos:
        return cls(
            organizations=InMemoryRepository("Organization", log),
            departments=InMemoryRepository("Department", log),
            roles=InMemoryRepository("Role", log),
            capabilities=InMemoryRepository("Capability", log),
            agents=InMemoryRepository("Agent", log),
            tasks=InMemoryRepository("Task", log),
            plans=InMemoryRepository("TaskPlan", log),
            nodes=InMemoryRepository("TaskNode", log),
            assignments=InMemoryRepository("TaskAssignment", log),
            artifacts=InMemoryRepository("Artifact", log),
            reviews=InMemoryRepository("Review", log),
            decisions=InMemoryRepository("Decision", log),
            actions=InMemoryRepository("Action", log),
            outcomes=InMemoryRepository("Outcome", log),
            lessons=InMemoryRepository("Lesson", log),
            schedules=InMemoryRepository("Schedule", log),
            responsibilities=ResponsibilityRepository(log),
        )


@dataclass
class Runtime:
    config: QORConfig
    machines: StateMachineRegistry
    log: InMemoryLogStore
    repos: Repos
    clock: Clock
    event_routes: EventRouteRepository = field(default_factory=EventRouteRepository)
    notifications: NotificationIntentRepository = field(
        default_factory=NotificationIntentRepository
    )
    refs: RefMinter = field(default_factory=RefMinter)
    organization_id: UUID | None = None
    # Set during bootstrap; used to resolve capability keys and role keys by name.
    capability_index: dict[str, UUID] = field(default_factory=dict)
    role_index: dict[str, UUID] = field(default_factory=dict)
    agent_index: dict[str, UUID] = field(default_factory=dict)

    @property
    def policies(self) -> Policies:
        return self.config.policies

    @classmethod
    def create(cls, *, clock: Clock | None = None, config: QORConfig | None = None) -> Runtime:
        cfg = config or QORConfig.load()
        log = InMemoryLogStore()
        return cls(
            config=cfg,
            machines=StateMachineRegistry.load(cfg.state_machine_dir),
            log=log,
            repos=Repos.build(log),
            clock=clock or SystemClock(),
        )

    # ---- helpers -------------------------------------------------------

    def now(self) -> datetime:
        return self.clock.now()

    def mint(self, prefix: str) -> tuple[UUID, str]:
        moment = self.clock.now()
        return new_id(at_ms=int(moment.timestamp() * 1000)), self.refs.mint(
            prefix, on=date(moment.year, moment.month, moment.day)
        )

    def organization(self) -> Organization:
        if self.organization_id is None:
            raise RuntimeError("Runtime has no organization; call bootstrap first")
        return self.repos.organizations.get(self.organization_id)

    def agent(self, key_or_id: str | UUID) -> Agent:
        if isinstance(key_or_id, UUID):
            return self.repos.agents.get(key_or_id)
        return self.repos.agents.get(self.agent_index[key_or_id])

    def capability(self, key: str) -> Capability:
        return self.repos.capabilities.get(self.capability_index[key])

    def role(self, key: str) -> Role:
        return self.repos.roles.get(self.role_index[key])

    def role_map(self) -> dict[UUID, Role]:
        return {r.id: r for r in self.repos.roles.all()}
