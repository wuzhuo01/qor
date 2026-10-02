"""Entity base class.

Every stateful QOR object carries the same spine: identity, a human-readable ref,
a status governed by a declared state machine, and an optimistic-locking version.

``status`` is a plain field here, which by itself would let anyone assign to it.
Invariant I7 ("no state changes without a StateTransition") is enforced at the
repository boundary instead: a save whose status differs from the stored status
and has no matching StateTransition is rejected. See
``qor.adapters.persistence.inmemory``.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from qor.kernel.actor import ActorRef

__all__ = ["Entity"]


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Name of the state machine that governs this entity, resolved through
    # StateMachineRegistry.for_entity(). Declared per subclass.
    ENTITY_TYPE: ClassVar[str] = ""
    REF_PREFIX: ClassVar[str] = "OBJ"

    id: UUID
    ref: str
    status: str
    version: int = 1
    created_at: datetime
    created_by: ActorRef
    updated_at: datetime
    labels: dict[str, str] = Field(default_factory=dict)

    @property
    def entity_type(self) -> str:
        if not self.ENTITY_TYPE:
            raise NotImplementedError(f"{type(self).__name__} must declare ENTITY_TYPE")
        return self.ENTITY_TYPE

    def __str__(self) -> str:
        return f"{self.ref}[{self.status}]"
