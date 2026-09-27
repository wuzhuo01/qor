"""QOR domain layer -- pure. No I/O, no LLM SDK, no workflow runtime (invariant I20).

``tests/test_architecture.py`` walks this package's imports and fails the build if
anything from an outer layer sneaks in.
"""

from qor.domain.action import Action, ActionCheck, ActionTarget, Verification
from qor.domain.artifact import Artifact, Finding
from qor.domain.audit import AuditRecord, Event, StateTransition
from qor.domain.base import Entity
from qor.domain.decision import (
    Alternative,
    Decision,
    MeasurementEvaluation,
    MeasurementPlan,
    SuccessCheck,
)
from qor.domain.enums import (
    ArtifactType,
    DecisionType,
    Environment,
    EventCategory,
    EventRouteResult,
    EventRouteStatus,
    FindingSeverity,
    NotificationActionability,
    NotificationDeliveryState,
    OperatingMode,
    ResponsibilitySource,
    ReviewType,
    RiskClass,
    RiskLevel,
    ScheduleCadence,
    ScheduleTickResult,
    TaskOrigin,
)
from qor.domain.event_route import RESOLVED_EVENT_NAME, EventRoute
from qor.domain.lesson import Lesson, LessonEvidence
from qor.domain.notification import NotificationIntent
from qor.domain.organization import (
    Agent,
    AgentCapability,
    Capability,
    Department,
    Organization,
    Role,
)
from qor.domain.outcome import Outcome
from qor.domain.responsibility import Responsibility
from qor.domain.review import Review, ReviewCondition, ReviewFinding
from qor.domain.schedule import Schedule
from qor.domain.task import (
    AgentSelection,
    NodeKind,
    ReviewPolicy,
    Task,
    TaskAssignment,
    TaskEdge,
    TaskNode,
    TaskPlan,
)
from qor.domain.values import Confidence, Constraints, EvidenceItem, Uncertainty

__all__ = [
    "Action",
    "ActionCheck",
    "ActionTarget",
    "Agent",
    "AgentCapability",
    "AgentSelection",
    "Alternative",
    "Artifact",
    "ArtifactType",
    "AuditRecord",
    "Capability",
    "Confidence",
    "Constraints",
    "Decision",
    "DecisionType",
    "Department",
    "Entity",
    "Environment",
    "Event",
    "EventCategory",
    "EventRoute",
    "EventRouteResult",
    "EventRouteStatus",
    "RESOLVED_EVENT_NAME",
    "EvidenceItem",
    "Finding",
    "FindingSeverity",
    "Lesson",
    "LessonEvidence",
    "NotificationActionability",
    "NotificationDeliveryState",
    "NotificationIntent",
    "MeasurementEvaluation",
    "MeasurementPlan",
    "SuccessCheck",
    "NodeKind",
    "OperatingMode",
    "Organization",
    "Outcome",
    "Review",
    "ReviewCondition",
    "ReviewFinding",
    "ReviewPolicy",
    "Responsibility",
    "ResponsibilitySource",
    "ReviewType",
    "RiskClass",
    "RiskLevel",
    "Role",
    "Schedule",
    "ScheduleCadence",
    "ScheduleTickResult",
    "StateTransition",
    "Task",
    "TaskAssignment",
    "TaskEdge",
    "TaskNode",
    "TaskOrigin",
    "TaskPlan",
    "Uncertainty",
    "Verification",
]
