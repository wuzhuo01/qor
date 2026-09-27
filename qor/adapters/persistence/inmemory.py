"""In-memory persistence.

This is a declared mock boundary: V0.1 keeps everything in process. The interfaces
are the real ones though, so swapping in PostgreSQL is an adapter change.

Two real guarantees are implemented here rather than deferred, because both are
architectural rather than infrastructural:

* **I7** -- a status change with no matching StateTransition is rejected.
* **Optimistic locking** -- a stale write is rejected instead of silently winning.

The append-only stores expose no update or delete method at all. That is the
enforcement: you cannot rewrite history through an API that has no such verb.
"""

from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from qor.domain.audit import GENESIS_HASH, AuditRecord, Event, StateTransition
from qor.kernel.errors import IllegalStateWrite, InvariantViolation, QORError

__all__ = ["InMemoryLogStore", "InMemoryRepository", "StaleWrite"]

T = TypeVar("T")


class StaleWrite(QORError):
    """Someone wrote based on a version that is no longer current."""


class InMemoryLogStore:
    """Append-only log for the three orthogonal records.

    All three share one sequence counter so that a single total order exists
    across them. Without that, "what happened, in order" needs timestamp
    tie-breaking, which is exactly the ambiguity an audit trail should not have.
    """

    def __init__(self) -> None:
        self._transitions: list[StateTransition] = []
        self._events: list[Event] = []
        self._audit: list[AuditRecord] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def append_transition(self, transition: StateTransition) -> StateTransition:
        self._transitions.append(transition)
        return transition

    def append_event(self, event: Event) -> Event:
        self._events.append(event)
        return event

    def append_audit(self, record: AuditRecord) -> AuditRecord:
        if record.record_hash != record.compute_hash():
            raise QORError("AuditRecord must be sealed before append")
        self._audit.append(record)
        return record

    def transitions(self) -> tuple[StateTransition, ...]:
        return tuple(self._transitions)

    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def audit_records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._audit)

    def transitions_for(self, entity_id: UUID) -> tuple[StateTransition, ...]:
        return tuple(t for t in self._transitions if t.entity_id == entity_id)

    def last_audit_hash(self) -> str:
        return self._audit[-1].record_hash if self._audit else GENESIS_HASH

    def verify_audit_chain(self) -> bool:
        """Recompute the whole chain. Any retroactive edit breaks it."""
        prev = GENESIS_HASH
        for record in self._audit:
            if record.prev_hash != prev or record.record_hash != record.compute_hash():
                return False
            prev = record.record_hash
        return True


class InMemoryRepository(Generic[T]):
    """Stores deep copies so callers cannot mutate persisted state by accident."""

    def __init__(self, entity_name: str, log: InMemoryLogStore) -> None:
        self._entity_name = entity_name
        self._log = log
        self._items: dict[UUID, T] = {}

    # ---- reads ---------------------------------------------------------

    def find(self, entity_id: UUID) -> T | None:
        found = self._items.get(entity_id)
        return found.model_copy(deep=True) if found is not None else None  # type: ignore[attr-defined]

    def get(self, entity_id: UUID) -> T:
        found = self.find(entity_id)
        if found is None:
            raise QORError(f"{self._entity_name} {entity_id} not found")
        return found

    def all(self) -> tuple[T, ...]:
        return tuple(item.model_copy(deep=True) for item in self._items.values())  # type: ignore[attr-defined]

    # ---- writes --------------------------------------------------------

    def insert(self, entity: T) -> T:
        eid: UUID = entity.id  # type: ignore[attr-defined]
        if eid in self._items:
            raise QORError(f"{self._entity_name} {eid} already exists")
        self._items[eid] = entity.model_copy(deep=True)  # type: ignore[attr-defined]
        return self.get(eid)

    def save(self, entity: T) -> T:
        eid: UUID = entity.id  # type: ignore[attr-defined]
        stored = self._items.get(eid)
        if stored is None:
            return self.insert(entity)

        incoming_version: int = entity.version  # type: ignore[attr-defined]
        stored_version: int = stored.version  # type: ignore[attr-defined]
        if incoming_version != stored_version:
            raise StaleWrite(
                f"{self._entity_name} {getattr(entity, 'ref', eid)}: wrote version "
                f"{incoming_version} but current is {stored_version}"
            )

        old_status: str = stored.status  # type: ignore[attr-defined]
        new_status: str = entity.status  # type: ignore[attr-defined]
        if old_status != new_status:
            self._require_transition(entity, old_status, new_status)
        if (
            getattr(stored, "ENTITY_TYPE", "") == "Decision"
            and old_status == "DECIDED"
            and getattr(stored, "measurement_plan", None)
            != getattr(entity, "measurement_plan", None)
        ):
            ref = getattr(entity, "ref", eid)
            raise InvariantViolation(
                "I-D4",
                f"{self._entity_name} {ref}: measurement contract is frozen after DECIDED",
            )

        bumped = entity.model_copy(deep=True, update={"version": stored_version + 1})  # type: ignore[attr-defined]
        self._items[eid] = bumped
        return self.get(eid)

    def _require_transition(self, entity: T, old_status: str, new_status: str) -> None:
        """Invariant I7. A status field is not an ordinary field."""
        eid: UUID = entity.id  # type: ignore[attr-defined]
        ref = getattr(entity, "ref", str(eid))
        history = self._log.transitions_for(eid)
        if not history:
            raise IllegalStateWrite(
                f"{self._entity_name} {ref}: {old_status} -> {new_status} with no "
                f"StateTransition recorded (I7)"
            )
        last = history[-1]
        if last.from_state != old_status or last.to_state != new_status:
            raise IllegalStateWrite(
                f"{self._entity_name} {ref}: attempted {old_status} -> {new_status} but the "
                f"last recorded transition was {last.from_state} -> {last.to_state} (I7)"
            )
        if not last.reason.strip():
            raise IllegalStateWrite(
                f"{self._entity_name} {ref}: transition {last.transition} has an empty reason (I7)"
            )
