"""Persistence ports.

``Repository.save`` is where invariant I7 is enforced: an entity whose ``status``
differs from the stored status, with no matching StateTransition, is rejected. That
placement is deliberate -- the domain cannot police itself against assignment, but
nothing reaches storage without passing through here.

``LogStore`` is append-only by contract. It exposes no update or delete.
"""

from __future__ import annotations

from typing import Protocol, TypeVar
from uuid import UUID

from qor.domain.audit import AuditRecord, Event, StateTransition

__all__ = ["LogStore", "Repository", "UnitOfWork"]

T = TypeVar("T")


class Repository(Protocol[T]):
    def get(self, entity_id: UUID) -> T: ...

    def find(self, entity_id: UUID) -> T | None: ...

    def save(self, entity: T) -> T: ...

    def all(self) -> tuple[T, ...]: ...


class LogStore(Protocol):
    """Append-only. Intentionally offers no mutation surface."""

    def append_transition(self, transition: StateTransition) -> StateTransition: ...

    def append_event(self, event: Event) -> Event: ...

    def append_audit(self, record: AuditRecord) -> AuditRecord: ...

    def transitions(self) -> tuple[StateTransition, ...]: ...

    def events(self) -> tuple[Event, ...]: ...

    def audit_records(self) -> tuple[AuditRecord, ...]: ...

    def transitions_for(self, entity_id: UUID) -> tuple[StateTransition, ...]: ...

    def next_seq(self) -> int: ...

    def last_audit_hash(self) -> str: ...


class UnitOfWork(Protocol):
    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, *exc: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
