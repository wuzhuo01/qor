"""QOR kernel: primitives shared by every layer. Standard library only."""

from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.clock import Clock, FrozenClock, SystemClock
from qor.kernel.errors import (
    AuthorizationError,
    IllegalStateWrite,
    InvariantViolation,
    QORError,
    TransitionRejected,
    UnknownTransition,
)
from qor.kernel.ids import RefMinter, new_id
from qor.kernel.statemachine import (
    GuardOutcome,
    StateMachine,
    StateMachineRegistry,
    StateSpec,
    TransitionSpec,
)

__all__ = [
    "ActorKind",
    "ActorRef",
    "AuthorizationError",
    "Clock",
    "FrozenClock",
    "GuardOutcome",
    "IllegalStateWrite",
    "InvariantViolation",
    "QORError",
    "RefMinter",
    "StateMachine",
    "StateMachineRegistry",
    "StateSpec",
    "SystemClock",
    "TransitionRejected",
    "TransitionSpec",
    "UnknownTransition",
    "new_id",
]
