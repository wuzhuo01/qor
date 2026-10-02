"""Responsibility -- who must handle one human-work fact.

This is not a Task owner, requester, sponsor, or the actor of a transition.
The human is whatever Agent.id was explicitly assigned, and that Agent must be
kind HUMAN. The state of the source does not choose the human.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from qor.domain.base import Entity
from qor.domain.enums import ResponsibilitySource

__all__ = ["Responsibility"]


class Responsibility(Entity):
    ENTITY_TYPE: ClassVar[str] = "Responsibility"
    REF_PREFIX: ClassVar[str] = "RESP"

    source_type: ResponsibilitySource
    source_id: UUID
    responsible_human_agent_id: UUID
    created_by_agent_id: UUID
    completed_at: datetime | None = None
    transferred_from_responsibility_id: UUID | None = None
    transfer_reason: str = ""
    cleared_at: datetime | None = None
    cleared_by_agent_id: UUID | None = None
    clear_reason: str = ""
