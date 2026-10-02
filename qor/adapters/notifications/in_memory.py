"""In-memory delivery. Records what it was given. No network and no retry worker."""

from __future__ import annotations

from qor.domain.notification import NotificationIntent
from qor.ports.notification import DeliveryResult

__all__ = ["InMemoryNotificationDelivery"]


class InMemoryNotificationDelivery:
    def __init__(self) -> None:
        self.attempts: list[NotificationIntent] = []
        self._fail_next = False

    def fail_next(self) -> None:
        self._fail_next = True

    def deliver(self, intent: NotificationIntent) -> DeliveryResult:
        self.attempts.append(intent)
        if self._fail_next:
            self._fail_next = False
            return DeliveryResult(ok=False, detail="forced delivery failure")
        return DeliveryResult(ok=True, detail="recorded in memory")
