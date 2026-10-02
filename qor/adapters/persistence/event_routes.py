"""In-memory EventRoute store.

EventRoute is not an Entity, so it does not go through the state-machine repository.
Status is a field. The SUCCESS receipt for (event_id, route_id) lives in the event log.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.enums import EventRouteStatus
from qor.domain.event_route import EventRoute
from qor.kernel.errors import QORError

__all__ = ["EventRouteRepository"]


class EventRouteRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, EventRoute] = {}

    def insert(self, route: EventRoute) -> EventRoute:
        if route.id in self._items:
            raise QORError(f"event route {route.ref} already exists")
        self._items[route.id] = route
        return route

    def get(self, route_id: UUID) -> EventRoute:
        try:
            return self._items[route_id]
        except KeyError as exc:
            raise QORError(f"no event route {route_id}") from exc

    def all(self) -> tuple[EventRoute, ...]:
        return tuple(self._items.values())

    def set_status(self, route_id: UUID, status: EventRouteStatus) -> EventRoute:
        current = self.get(route_id)
        updated = current.model_copy(update={"status": status})
        self._items[route_id] = updated
        return updated
