"""TransitionEngine -- the only way any entity's status changes.

Everything invariants I7 and I8 promise happens here, in one place:

1. resolve the transition against the declarative machine
2. authorize the actor if the transition declares a permission
3. evaluate guards and keep their results
4. move the status, bump the version
5. append a StateTransition
6. emit an Event if the transition declares one

No service is allowed to assign to ``entity.status`` directly. The repository
rejects such a write, so this is enforced rather than merely documented.
"""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from qor.domain.audit import Event, StateTransition
from qor.domain.enums import EventCategory
from qor.kernel.actor import ActorRef
from qor.kernel.errors import TransitionRejected
from qor.kernel.statemachine import GuardOutcome
from qor.services.authorization import Authorizer
from qor.services.guards import GuardContext, GuardRegistry
from qor.services.runtime import Runtime

__all__ = ["TransitionEngine", "TransitionResult"]

E = TypeVar("E")


class TransitionResult:
    def __init__(self, entity: Any, transition: StateTransition, event: Event | None) -> None:
        self.entity = entity
        self.transition = transition
        self.event = event


class TransitionEngine:
    def __init__(
        self,
        runtime: Runtime,
        authorizer: Authorizer,
        guards: GuardRegistry | None = None,
    ) -> None:
        self._rt = runtime
        self._authz = authorizer
        self._guards = guards or GuardRegistry.default()

    # ---- creation ------------------------------------------------------

    def record_creation(
        self,
        entity: Any,
        *,
        actor: ActorRef,
        reason_code: str,
        reason: str,
        correlation_id: UUID | None = None,
        event_name: str = "",
        payload: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Log an entity coming into existence.

        Creation is not a transition between two states, so it gets
        ``from_state=None``. Without this record, an entity's history would begin
        mid-story.
        """
        machine = self._rt.machines.for_entity(entity.entity_type)
        if entity.status != machine.initial:
            raise TransitionRejected(
                machine.name,
                "<create>",
                f"new {entity.entity_type} must start at '{machine.initial}', got '{entity.status}'",
            )
        transition = self._rt.log.append_transition(
            StateTransition(
                seq=self._rt.log.next_seq(),
                entity_type=entity.entity_type,
                entity_id=entity.id,
                entity_ref=entity.ref,
                from_state=None,
                to_state=entity.status,
                transition="create",
                actor=actor,
                reason_code=reason_code,
                reason=reason,
                occurred_at=self._rt.clock.now(),
                correlation_id=correlation_id,
            )
        )
        event = None
        if event_name:
            event = self._emit(
                event_name,
                entity=entity,
                actor=actor,
                payload=payload or {},
                correlation_id=correlation_id,
                causation_id=transition.id,
            )
        return TransitionResult(entity, transition, event)

    # ---- transitions ---------------------------------------------------

    def evaluate(
        self,
        entity: Any,
        transition: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> tuple[bool, tuple[GuardOutcome, ...]]:
        """Run the guards without changing anything.

        Used by the orchestrator to ask "is this task ready to advance?" instead of
        firing transitions speculatively and catching exceptions as control flow.
        """
        machine = self._rt.machines.for_entity(entity.entity_type)
        spec = machine.spec(transition)
        if machine.is_terminal(entity.status) or not spec.accepts(entity.status):
            return False, (
                GuardOutcome(
                    "source_state",
                    False,
                    f"'{transition}' not available from '{entity.status}'",
                ),
            )
        outcomes = self._run_guards(spec.guards, entity, extra or {})
        return all(o.passed for o in outcomes), outcomes

    def fire(
        self,
        entity: Any,
        transition: str,
        *,
        actor: ActorRef,
        reason_code: str,
        reason: str,
        extra: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        metadata: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TransitionResult:
        machine = self._rt.machines.for_entity(entity.entity_type)
        spec = machine.resolve(transition, entity.status)
        machine.validate_reason(transition, reason_code, reason)

        if spec.permission:
            self._authz.check(
                actor=actor,
                permission=spec.permission,
                target_type=entity.entity_type,
                target_id=entity.id,
                target_ref=entity.ref,
                correlation_id=correlation_id,
            )

        outcomes = self._run_guards(spec.guards, entity, extra or {})
        failed = [o for o in outcomes if not o.passed]
        if failed:
            raise TransitionRejected(
                machine.name,
                transition,
                "; ".join(f"{o.name}: {o.detail or 'failed'}" for o in failed),
            )

        target = spec.target
        if spec.returns_to_previous:
            target = self._previous_state(entity, spec.source[0])

        from_state = entity.status
        entity.status = target
        entity.updated_at = self._rt.clock.now()

        record = self._rt.log.append_transition(
            StateTransition(
                seq=self._rt.log.next_seq(),
                entity_type=entity.entity_type,
                entity_id=entity.id,
                entity_ref=entity.ref,
                from_state=from_state,
                to_state=target,
                transition=transition,
                actor=actor,
                reason_code=reason_code,
                reason=reason,
                guard_results=tuple(o.as_dict() for o in outcomes),
                occurred_at=entity.updated_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
                metadata=metadata or {},
            )
        )

        event = None
        if spec.emits:
            event = self._emit(
                spec.emits,
                entity=entity,
                actor=actor,
                payload={
                    "from_state": from_state,
                    "to_state": target,
                    "reason_code": reason_code,
                    **(payload or {}),
                },
                correlation_id=correlation_id,
                causation_id=record.id,
            )
        return TransitionResult(entity, record, event)

    def emit(
        self,
        name: str,
        *,
        entity: Any,
        actor: ActorRef,
        payload: dict[str, Any],
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
    ) -> Event:
        """Record an organizational fact that is not itself a state change.

        This is Event emit, not an Event Router. Nothing subscribes.
        """
        return self._emit(
            name,
            entity=entity,
            actor=actor,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    # ---- internals -----------------------------------------------------

    def _run_guards(
        self, names: tuple[str, ...], entity: Any, extra: dict[str, Any]
    ) -> tuple[GuardOutcome, ...]:
        ctx = GuardContext(runtime=self._rt, entity=entity, extra=extra)
        return tuple(self._guards.run(name, ctx) for name in names)

    def _previous_state(self, entity: Any, exceptional_state: str) -> str:
        """Where to return to after leaving an exceptional state.

        Reads it back out of the transition log rather than storing a
        ``previous_state`` field: the log is already the authority on history, and a
        cached copy would be one more thing that can disagree with it.
        """
        history = self._rt.log.transitions_for(entity.id)
        for record in reversed(history):
            if record.to_state == exceptional_state and record.from_state:
                return record.from_state
        raise TransitionRejected(
            entity.entity_type,
            "unblock",
            f"no recorded state preceding '{exceptional_state}' for {entity.ref}",
        )

    def _emit(
        self,
        name: str,
        *,
        entity: Any,
        actor: ActorRef,
        payload: dict[str, Any],
        correlation_id: UUID | None,
        causation_id: UUID | None,
    ) -> Event:
        return self._rt.log.append_event(
            Event(
                seq=self._rt.log.next_seq(),
                name=name,
                category=_categorise(name),
                aggregate_type=entity.entity_type,
                aggregate_id=entity.id,
                aggregate_ref=entity.ref,
                payload=payload,
                emitted_by=actor,
                occurred_at=self._rt.clock.now(),
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        )


def _categorise(name: str) -> EventCategory:
    if any(token in name for token in ("blocked", "escalated", "failed", "timed_out")):
        return EventCategory.EXCEPTION
    if any(
        token in name
        for token in (
            "approv",
            "reject",
            "permission",
            "risk",
            "decision",
            "review",
            "audit",
            "outcome",
            "follow_through",
            "lesson",
        )
    ):
        return EventCategory.GOVERNANCE
    if any(token in name for token in ("assignment", "delegation")):
        return EventCategory.DELEGATION
    return EventCategory.LIFECYCLE
