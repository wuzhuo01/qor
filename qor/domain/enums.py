"""Domain enumerations.

RiskLevel and RiskClass are ordered on purpose: policy rules are expressed as
comparisons ("anything at or above ELEVATED needs a sponsor"), and an unordered
enum would push that ordering into scattered if-chains.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = [
    "ArtifactType",
    "ClaimClass",
    "DecisionType",
    "Environment",
    "EvidenceSource",
    "EventCategory",
    "EventRouteResult",
    "EventRouteStatus",
    "FindingSeverity",
    "NotificationActionability",
    "NotificationDeliveryState",
    "OperatingMode",
    "ResponsibilitySource",
    "ReviewType",
    "RiskClass",
    "RiskLevel",
    "TaskOrigin",
]


class RiskLevel(IntEnum):
    """Task risk. Ordered so policies can be written as thresholds."""

    ROUTINE = 0
    ELEVATED = 1
    HIGH = 2
    CRITICAL = 3

    @classmethod
    def parse(cls, value: str) -> RiskLevel:
        return cls[value.upper()]

    def __str__(self) -> str:
        return self.name


class RiskClass(IntEnum):
    """Capability risk class -- how close a capability is to changing reality.

    REALITY_AFFECTING is the class that forces work through the Action Gateway.
    Downgrading it is a HUMAN-CIO-only permission (invariant I26) precisely
    because it would otherwise be a legal bypass of the gateway.
    """

    READ_ONLY = 0
    ADVISORY = 1
    SIMULATION_AFFECTING = 2
    REALITY_AFFECTING = 3

    @classmethod
    def parse(cls, value: str) -> RiskClass:
        return cls[value.upper()]

    def __str__(self) -> str:
        return self.name


class OperatingMode(StrEnum):
    NORMAL = "NORMAL"
    CAUTIOUS = "CAUTIOUS"
    DEFENSIVE = "DEFENSIVE"
    HALTED = "HALTED"


class TaskOrigin(StrEnum):
    """How a Task was opened. Each value has one door.

    EVENT means an Event Router opened the Task from an EventRoute.
    It does not mean the requester was non-human.
    """

    HUMAN = "HUMAN"
    SCHEDULE = "SCHEDULE"
    EVENT = "EVENT"
    DECOMPOSITION = "DECOMPOSITION"
    REVIEW_FINDING = "REVIEW_FINDING"
    FOLLOW_THROUGH = "FOLLOW_THROUGH"


class ScheduleCadence(StrEnum):
    ONCE = "ONCE"
    EVERY = "EVERY"


class ResponsibilitySource(StrEnum):
    """Which existing fact a Responsibility binds. Decision is not a runtime source."""

    TASK = "TASK"
    ACTION = "ACTION"
    TASK_NODE = "TASK_NODE"


class NotificationActionability(StrEnum):
    """v0.1 has one kind of intent: a human still has to act."""

    ACTION_REQUIRED = "ACTION_REQUIRED"


class NotificationDeliveryState(StrEnum):
    """Delivery bookkeeping. FAILED is not a Task, Action, or Responsibility failure."""

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class EventRouteStatus(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class EventRouteResult(StrEnum):
    """Receipt written on ``qor.event_route.resolved``.

    Only SUCCESS counts as handled. SKIPPED_DISABLED leaves the pair open so a
    later ENABLED route can still open a Task.
    """

    SUCCESS = "SUCCESS"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"


class ScheduleTickResult(StrEnum):
    CREATED = "CREATED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_ACTIVE = "SKIPPED_ACTIVE"
    SKIPPED_DISABLED = "SKIPPED_DISABLED"
    NOT_DUE = "NOT_DUE"


class ClaimClass(StrEnum):
    """What kind of statement a finding is.

    This is not an industry category. It only separates a factual claim from a
    hypothesis, a speculation, a research direction, or a human input.
    """

    HYPOTHESIS = "hypothesis"
    SPECULATION = "speculation"
    DIRECTION = "direction"
    FACT = "fact"
    INPUT = "input"


class EvidenceSource(StrEnum):
    """Where an EvidenceItem came from.

    Historical artifacts may still carry an older kind label. Those labels stay
    loadable. A value outside this set and outside that historical list is rejected.
    """

    REASONING = "reasoning"
    EXTERNAL = "external"
    TOOL = "tool"
    HUMAN = "human"


class ArtifactType(StrEnum):
    ANALYSIS_REPORT = "ANALYSIS_REPORT"
    ANALYSIS_RESULT = "ANALYSIS_RESULT"
    RISK_ASSESSMENT = "RISK_ASSESSMENT"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    SYNTHESIS = "SYNTHESIS"
    RECOMMENDATION = "RECOMMENDATION"
    PLAN_RATIONALE = "PLAN_RATIONALE"


class ReviewType(StrEnum):
    METHODOLOGY = "METHODOLOGY"
    EVIDENCE = "EVIDENCE"
    RISK = "RISK"
    COMPLIANCE = "COMPLIANCE"
    PEER = "PEER"


class FindingSeverity(IntEnum):
    INFO = 0
    MINOR = 1
    MAJOR = 2
    BLOCKER = 3

    def __str__(self) -> str:
        return self.name


class DecisionType(StrEnum):
    NO_ACTION = "NO_ACTION"
    RECOMMEND = "RECOMMEND"
    PARAMETER_CHANGE = "PARAMETER_CHANGE"
    WORKFLOW_PAUSE = "WORKFLOW_PAUSE"
    POSITION_CHANGE = "POSITION_CHANGE"
    ESCALATE = "ESCALATE"


class Environment(StrEnum):
    SANDBOX = "SANDBOX"
    PAPER = "PAPER"
    LIVE = "LIVE"


class EventCategory(StrEnum):
    LIFECYCLE = "LIFECYCLE"
    GOVERNANCE = "GOVERNANCE"
    EXCEPTION = "EXCEPTION"
    DELEGATION = "DELEGATION"
