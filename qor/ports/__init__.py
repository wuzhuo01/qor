"""Ports: the interfaces the domain and services depend on.

Adapters implement these. Nothing in here knows about SQL, HTTP, LangGraph or any
LLM vendor.
"""

from qor.ports.agent_runtime import (
    AgentRuntimePort,
    ReviewOpinion,
    ReviewRequest,
    WorkRequest,
    WorkResult,
)
from qor.ports.llm import LLMPort, LLMUnavailable
from qor.ports.notification import DeliveryResult, NotificationPort
from qor.ports.planner import NodeDraft, PlanDraft, PlannerPort, PlanRequest
from qor.ports.repositories import LogStore, Repository, UnitOfWork
from qor.ports.world import WorldReader, WorldWriter

__all__ = [
    "AgentRuntimePort",
    "DeliveryResult",
    "LLMPort",
    "LLMUnavailable",
    "NotificationPort",
    "LogStore",
    "NodeDraft",
    "PlanDraft",
    "PlanRequest",
    "PlannerPort",
    "Repository",
    "ReviewOpinion",
    "ReviewRequest",
    "UnitOfWork",
    "WorkRequest",
    "WorkResult",
    "WorldReader",
    "WorldWriter",
]
