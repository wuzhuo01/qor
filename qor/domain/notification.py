"""NotificationIntent -- one request that a responsible human notice a fact.

This is not an Event, an AuditRecord, or a Responsibility. It does not choose
the human and it does not own the source's state. Delivery state is bookkeeping
for the port, not an organizational state machine.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from qor.domain.enums import (
    NotificationActionability,
    NotificationDeliveryState,
    ResponsibilitySource,
)

__all__ = ["NotificationIntent"]


class NotificationIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    notification_key: str
    responsibility_id: UUID
    target_human_agent_id: UUID
    source_type: ResponsibilitySource
    source_id: UUID
    source_status: str
    actionability: NotificationActionability = NotificationActionability.ACTION_REQUIRED
    reason: str
    occurred_at: datetime
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    state: NotificationDeliveryState = NotificationDeliveryState.NOT_ATTEMPTED
