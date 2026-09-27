"""Notification delivery port.

The service depends on this, not on a channel. Email, chat, and webhooks are
not part of v0.1.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from qor.domain.notification import NotificationIntent

__all__ = ["DeliveryResult", "NotificationPort"]


class DeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    detail: str = ""


class NotificationPort(Protocol):
    def deliver(self, intent: NotificationIntent) -> DeliveryResult:
        """Hand one intent to a delivery adapter. Do not change organizational state."""
