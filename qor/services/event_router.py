"""Event Router -- match an Event to EventRoutes and stop at Task OPEN.

It calls Intake and stops. It does not plan, assign, review, decide, or act.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from qor.domain.audit import Event
from qor.domain.enums import EventRouteResult, EventRouteStatus
from qor.domain.event_route import RESOLVED_EVENT_NAME, EventRoute
from qor.kernel.actor import ActorRef
from qor.kernel.errors import QORError
from qor.services.intake import IntakeService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["EventRouter", "RouteResult"]


def _is_resolved_receipt(name: str) -> bool:
    return name == RESOLVED_EVENT_NAME


class RouteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route_id: UUID
    route_ref: str
    task_ref: str
    result: EventRouteResult


class EventRouter:
    def __init__(self, runtime: Runtime, engine: TransitionEngine, intake: IntakeService) -> None:
        self._rt = runtime
        self._engine = engine
        self._intake = intake
        self._actor = ActorRef.system("QOR-EVENT-ROUTER")

    def register(
        self,
        *,
        event_name: str,
        objective: str,
        status: EventRouteStatus = EventRouteStatus.ENABLED,
    ) -> EventRoute:
        rid, ref = self._rt.mint("ROUTE")
        route = EventRoute(
            id=rid,
            ref=ref,
            status=status,
            event_name=event_name,
            objective=objective,
        )
        return self._rt.event_routes.insert(route)

    def set_status(self, route_id: UUID, status: EventRouteStatus) -> EventRoute:
        return self._rt.event_routes.set_status(route_id, status)

    def route(self, event: Event) -> tuple[RouteResult, ...]:
        """Match enabled routes and open Tasks. A resolved receipt is not an input."""
        if _is_resolved_receipt(event.name):
            return ()
        results: list[RouteResult] = []
        for route in self._rt.event_routes.all():
            if route.event_name != event.name:
                continue
            results.append(self._handle(event, route))
        return tuple(results)

    def _handle(self, event: Event, route: EventRoute) -> RouteResult:
        # Idempotency key is (event.id, route.id). route.ref is only a label.
        if self._already_succeeded(event.id, route.id):
            self._record(event, route, EventRouteResult.SKIPPED_DUPLICATE, "")
            return RouteResult(
                route_id=route.id,
                route_ref=route.ref,
                task_ref="",
                result=EventRouteResult.SKIPPED_DUPLICATE,
            )
        if route.status is EventRouteStatus.DISABLED:
            self._record(event, route, EventRouteResult.SKIPPED_DISABLED, "")
            return RouteResult(
                route_id=route.id,
                route_ref=route.ref,
                task_ref="",
                result=EventRouteResult.SKIPPED_DISABLED,
            )
        task = self._intake.open_from_event(route, event)
        if task.status != "OPEN":
            raise QORError(f"event route {route.ref} did not admit a task to OPEN")
        self._record(event, route, EventRouteResult.SUCCESS, task.ref)
        return RouteResult(
            route_id=route.id,
            route_ref=route.ref,
            task_ref=task.ref,
            result=EventRouteResult.SUCCESS,
        )

    def _already_succeeded(self, event_id: UUID, route_id: UUID) -> bool:
        want_event = str(event_id)
        want_route = str(route_id)
        for recorded in self._rt.log.events():
            if recorded.name != RESOLVED_EVENT_NAME:
                continue
            payload = recorded.payload
            if payload.get("result") != EventRouteResult.SUCCESS.value:
                continue
            if payload.get("event_id") == want_event and payload.get("route_id") == want_route:
                return True
        return False

    def _record(
        self,
        event: Event,
        route: EventRoute,
        result: EventRouteResult,
        task_ref: str,
    ) -> None:
        self._engine.emit(
            RESOLVED_EVENT_NAME,
            entity=self._rt.organization(),
            actor=self._actor,
            payload={
                "event_id": str(event.id),
                "route_id": str(route.id),
                "route_ref": route.ref,
                "task_ref": task_ref,
                "result": result.value,
            },
            correlation_id=event.id,
        )
