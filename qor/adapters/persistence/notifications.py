"""In-memory NotificationIntent store.

Not an entity repository: delivery state is not a state-machine transition.
Identity fields stay as they were on the first save.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.notification import NotificationIntent
from qor.kernel.errors import QORError

__all__ = ["NotificationIntentRepository"]

_LOCKED = (
    "notification_key",
    "responsibility_id",
    "target_human_agent_id",
    "source_type",
    "source_id",
    "source_status",
    "actionability",
    "reason",
    "occurred_at",
    "correlation_id",
    "causation_id",
)


class NotificationIntentRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, NotificationIntent] = {}

    def save(self, intent: NotificationIntent) -> NotificationIntent:
        current = self._items.get(intent.id)
        if current is None:
            if self.find_by_key(intent.notification_key) is not None:
                raise QORError(f"notification key {intent.notification_key} already has an intent")
            self._items[intent.id] = intent
            return intent
        for name in _LOCKED:
            if getattr(current, name) != getattr(intent, name):
                raise QORError(f"notification {intent.id} cannot change {name}")
        self._items[intent.id] = intent
        return intent

    def get(self, intent_id: UUID) -> NotificationIntent:
        try:
            return self._items[intent_id]
        except KeyError as exc:
            raise QORError(f"no notification {intent_id}") from exc

    def find_by_key(self, notification_key: str) -> NotificationIntent | None:
        for item in self._items.values():
            if item.notification_key == notification_key:
                return item
        return None

    def find_by_responsibility(self, responsibility_id: UUID) -> tuple[NotificationIntent, ...]:
        return tuple(
            item for item in self._items.values() if item.responsibility_id == responsibility_id
        )

    def all(self) -> tuple[NotificationIntent, ...]:
        return tuple(self._items.values())
