"""Application wiring.

The only place adapters are chosen. Everything above this line depends on ports, so
swapping the planner, the agent runtime or the persistence layer happens here and
nowhere else.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from qor.adapters.agents.generic import GenericAgentRuntime
from qor.adapters.llm.openai_compatible import from_environment
from qor.adapters.notifications.in_memory import InMemoryNotificationDelivery
from qor.adapters.planner.llm import LLMPlanner
from qor.adapters.planner.stub import StubPlanner
from qor.adapters.timeline import Timeline
from qor.adapters.world.filesystem import (
    FilesystemSandboxWorldReader,
    FilesystemSandboxWorldWriter,
)
from qor.kernel.clock import Clock, FrozenClock
from qor.ports.agent_runtime import AgentRuntimePort
from qor.ports.llm import LLMUnavailable
from qor.ports.planner import PlannerPort
from qor.services.authorization import Authorizer
from qor.services.bootstrap import bootstrap
from qor.services.config import QORConfig
from qor.services.notification import NotificationService
from qor.services.orchestrator import Orchestrator
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["QORApp", "build_app", "resolve_planner"]

# Kept so existing callers can pass an explicit weaken set. The default
# organization does not name a business capability to weaken.
DEFAULT_WEAKENED = frozenset()


@dataclass
class QORApp:
    runtime: Runtime
    engine: TransitionEngine
    authorizer: Authorizer
    orchestrator: Orchestrator
    timeline: Timeline
    planner: PlannerPort
    agents: AgentRuntimePort
    notifications: NotificationService
    notification_delivery: InMemoryNotificationDelivery


def resolve_planner(kind: str = "auto") -> PlannerPort:
    """Pick a planner.

    ``llm`` never falls back: a missing provider is a failed V0.2 proof, not a
    stub run wearing an llm badge. ``stub`` is for CI. ``auto`` may fall back to
    the stub when unconfigured, but that run is not V0.2 evidence.
    """
    if kind == "stub":
        return StubPlanner()
    if kind == "llm":
        return LLMPlanner(from_environment())
    if kind == "auto":
        try:
            return LLMPlanner(from_environment())
        except LLMUnavailable:
            print(
                "WARNING: no LLM provider configured; using StubPlanner. "
                "This run is not V0.2 Real AI CIO evidence.",
                file=sys.stderr,
            )
            return StubPlanner()
    raise ValueError(f"unknown planner kind '{kind}'")


def build_app(
    *,
    planner: PlannerPort | None = None,
    agents: AgentRuntimePort | None = None,
    clock: Clock | None = None,
    weaken_first_attempt_for: frozenset[str] = DEFAULT_WEAKENED,
    world_root: Path | None = None,
    world_write_fault: str | None = None,
    world_read_fault: str | None = None,
    config: QORConfig | None = None,
) -> QORApp:
    the_clock = clock or FrozenClock()
    runtime, engine, authorizer = bootstrap(Runtime.create(clock=the_clock, config=config))
    the_planner = planner or resolve_planner()
    the_agents = agents or GenericAgentRuntime(the_clock)
    _ = weaken_first_attempt_for
    world_writer = (
        FilesystemSandboxWorldWriter(world_root, write_fault=world_write_fault)
        if world_root is not None
        else None
    )
    world_reader = (
        FilesystemSandboxWorldReader(world_root, read_fault=world_read_fault)
        if world_root is not None
        else None
    )
    delivery = InMemoryNotificationDelivery()
    notifications = NotificationService(runtime, delivery)
    orchestrator = Orchestrator(
        runtime,
        engine,
        authorizer,
        the_planner,
        the_agents,
        world_writer=world_writer,
        world_reader=world_reader,
    )
    orchestrator.responsibilities.attach_notification_service(notifications)
    return QORApp(
        runtime=runtime,
        engine=engine,
        authorizer=authorizer,
        orchestrator=orchestrator,
        timeline=Timeline(runtime.log),
        planner=the_planner,
        agents=the_agents,
        notifications=notifications,
        notification_delivery=delivery,
    )
