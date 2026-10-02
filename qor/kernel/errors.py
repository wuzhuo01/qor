"""QOR error taxonomy.

Errors are part of the domain language: a rejected transition or a denied
permission is a normal organizational event, not a crash. Each carries enough
structure to be written into a StateTransition or AuditRecord.
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "AuthorizationError",
    "CapabilityGap",
    "EntryRejected",
    "IllegalStateWrite",
    "InvariantViolation",
    "QORError",
    "TransitionRejected",
    "UnknownTransition",
]


class QORError(Exception):
    """Base class for every QOR error."""


class InvariantViolation(QORError):
    """A domain invariant (I1..I27) was violated."""

    def __init__(self, invariant: str, detail: str) -> None:
        self.invariant = invariant
        self.detail = detail
        super().__init__(f"[{invariant}] {detail}")


class UnknownTransition(QORError):
    """No transition with this name exists on the machine."""


class TransitionRejected(QORError):
    """A transition exists but its guards or source state did not permit it."""

    def __init__(self, machine: str, transition: str, reason: str) -> None:
        self.machine = machine
        self.transition = transition
        self.reason = reason
        super().__init__(f"{machine}.{transition} rejected: {reason}")


class IllegalStateWrite(QORError):
    """A status field changed without a corresponding StateTransition (I7)."""


class AuthorizationError(QORError):
    """The actor lacks the permission required for this operation (I6)."""

    def __init__(self, actor: str, permission: str, detail: str = "") -> None:
        self.actor = actor
        self.permission = permission
        super().__init__(f"{actor} lacks '{permission}'{f': {detail}' if detail else ''}")


class EntryRejected(QORError):
    """run_task was called from a state that is not an execution entry.

    Raised before planning or execution. It does not change the task.
    """

    def __init__(self, task_id: UUID, state: str, reason: str) -> None:
        self.task_id = task_id
        self.state = state
        self.reason = reason
        super().__init__(f"run_task rejected for {task_id} in {state}: {reason}")


class CapabilityGap(QORError):
    """No agent in the organization holds the required capability (T11).

    This is deliberately an error rather than a silent downgrade: picking a
    "close enough" agent would violate Capability First.
    """

    def __init__(self, capabilities: list[str], detail: str = "") -> None:
        self.capabilities = capabilities
        super().__init__(
            f"no agent holds required capabilities {capabilities}{f': {detail}' if detail else ''}"
        )
