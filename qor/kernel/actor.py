"""ActorRef -- the polymorphic "who did this" value object.

Ruling Q1: humans and AI agents live in the same Agent registry, distinguished by
``Agent.kind``. ActorRef is therefore uniform across delegation, review, approval
and audit, and QOR code never branches on ``if is_human``.

``role_id`` is not decoration: the same agent acting under a different Role has
different permissions, and audit always asks "under what capacity did they act".
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["ActorKind", "ActorRef"]


class ActorKind(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    RULE = "RULE"
    EXTERNAL = "EXTERNAL"


class ActorRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    id: UUID | None = None
    role_id: UUID | None = None
    display: str = ""

    @classmethod
    def system(cls, display: str = "SYSTEM") -> ActorRef:
        return cls(kind=ActorKind.SYSTEM, display=display)

    @property
    def is_machine_authority(self) -> bool:
        """True for actors that are not accountable principals.

        SYSTEM and RULE may drive state transitions but can never hold approval
        authority; that is checked in the authorization service.
        """
        return self.kind in (ActorKind.SYSTEM, ActorKind.RULE)

    def __str__(self) -> str:
        return self.display or f"{self.kind}:{self.id}"
