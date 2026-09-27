"""EventRoute -- which event name opens which objective. Not a workflow."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from qor.domain.enums import EventRouteStatus
from qor.kernel.errors import InvariantViolation

__all__ = ["RESOLVED_EVENT_NAME", "EventRoute"]

# Audit fact written by the router. It is not a route input.
RESOLVED_EVENT_NAME = "qor.event_route.resolved"


class EventRoute(BaseModel):
    """A named rule: Event.name equals event_name, then open objective.

    Not an Entity. Status is ENABLED or DISABLED only. There is no state machine,
    condition, priority, handler, or subscription.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    ref: str
    status: EventRouteStatus
    event_name: str
    objective: str

    @model_validator(mode="after")
    def _rule_is_a_name_match(self) -> Self:
        if not self.event_name.strip():
            raise InvariantViolation("I-ER1", "EventRoute.event_name must be non-empty")
        if self.event_name == RESOLVED_EVENT_NAME:
            raise InvariantViolation(
                "I-ER1",
                "EventRoute cannot match qor.event_route.resolved",
            )
        if not self.objective.strip():
            raise InvariantViolation("I-ER1", "EventRoute.objective must be non-empty")
        return self
