"""Three orthogonal append-only logs.

They are frequently conflated, so it is worth being explicit about the different
question each one answers:

* **StateTransition** -- "what changed, and why". Reconstructs any entity's history.
* **Event** -- "what should the organization react to". A fact worth telling others.
* **AuditRecord** -- "was this authorized, under which policy". Hash-chained.

One transition can produce all three. Merging them would mean losing either the
replay guarantee, the reaction semantics, or the tamper evidence.

V0.1 implements Event *records* and Event *emission*. It does not implement an
Event Router, a rule engine, or Event -> Task triggering (Human CIO ruling,
correction 2). Nothing here may be replaced by a print statement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from qor.domain.enums import EventCategory
from qor.kernel.actor import ActorRef

__all__ = ["AuditRecord", "Event", "StateTransition"]

GENESIS_HASH = "0" * 64


class StateTransition(BaseModel):
    """Immutable record of one state change.

    ``guard_results`` is kept even on success: knowing which conditions were
    checked is what lets someone later ask "would this still pass today?".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    seq: int
    entity_type: str
    entity_id: UUID
    entity_ref: str
    from_state: str | None
    to_state: str
    transition: str
    actor: ActorRef
    reason_code: str
    reason: str
    guard_results: tuple[dict[str, Any], ...] = ()
    occurred_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    def __str__(self) -> str:
        origin = self.from_state or "<new>"
        return (
            f"#{self.seq:04d} {self.entity_type} {self.entity_ref} "
            f"{origin} -> {self.to_state} ({self.transition}) "
            f"by {self.actor} [{self.reason_code}]"
        )


class Event(BaseModel):
    """A fact the organization publishes about itself.

    Namespaced (``qor.task.opened``). The object stays frozen. Event Router v0.1
    reads events; it does not mutate them and it does not set ``routed``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    seq: int
    name: str
    category: EventCategory
    aggregate_type: str
    aggregate_id: UUID
    aggregate_ref: str
    payload: dict[str, Any] = Field(default_factory=dict)
    emitted_by: ActorRef
    occurred_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    # Unused by Event Router v0.1. Idempotency is the resolved receipt, not this flag.
    routed: bool = False

    def __str__(self) -> str:
        return f"#{self.seq:04d} {self.name} <- {self.aggregate_ref}"


class AuditRecord(BaseModel):
    """Hash-chained authorization record.

    The chain is what makes retroactive editing detectable. Without it, an audit
    log is only as trustworthy as write access to the database.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    seq: int
    actor: ActorRef
    permission: str
    target_type: str
    target_id: UUID | None = None
    target_ref: str = ""
    allowed: bool
    policy_ref: str = ""
    basis: str = ""
    occurred_at: datetime
    correlation_id: UUID | None = None
    prev_hash: str = GENESIS_HASH
    record_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "seq": self.seq,
                "actor": f"{self.actor.kind}:{self.actor.id}",
                "permission": self.permission,
                "target": f"{self.target_type}:{self.target_ref}",
                "allowed": self.allowed,
                "policy_ref": self.policy_ref,
                "basis": self.basis,
                "occurred_at": self.occurred_at.isoformat(),
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def sealed(self) -> AuditRecord:
        return self.model_copy(update={"record_hash": self.compute_hash()})

    def __str__(self) -> str:
        outcome = "ALLOW" if self.allowed else "DENY "
        return f"#{self.seq:04d} {outcome} {self.actor} {self.permission} -> {self.target_ref}"
