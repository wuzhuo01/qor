"""Intake -- the only door into the organization (invariant I1).

A human objective is not work until it is a Task. Nothing downstream accepts a
free-form string, which is what stops "just ask the agent" from becoming a second,
unaudited entry path.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.audit import Event
from qor.domain.enums import ArtifactType, RiskLevel, TaskOrigin
from qor.domain.event_route import EventRoute
from qor.domain.outcome import Outcome
from qor.domain.schedule import Schedule
from qor.domain.task import Task
from qor.domain.values import Constraints
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import QORError
from qor.services.authorization import Authorizer
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["IntakeService"]


class IntakeService:
    def __init__(self, runtime: Runtime, engine: TransitionEngine, authorizer: Authorizer) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer

    def submit(
        self,
        *,
        objective: str,
        title: str = "",
        requested_by_agent_id: UUID,
        risk_level: RiskLevel = RiskLevel.ROUTINE,
        required_outputs: tuple[ArtifactType, ...] = (),
        constraints: Constraints | None = None,
        owner_agent_id: UUID | None = None,
        sponsor_agent_id: UUID | None = None,
    ) -> Task:
        """Create a Task in DRAFT and admit it to OPEN.

        Admission is a separate, permissioned step rather than part of creation:
        anyone may propose work, but admitting it into the organization's queue is
        an act of authority and is audited as one.
        """
        requester = self._rt.repos.agents.get(requested_by_agent_id)
        if requester.kind is not ActorKind.HUMAN:
            raise QORError(
                "intake.submit is the HUMAN origin door; "
                "a non-human requester is not TaskOrigin.EVENT"
            )
        owner_id = owner_agent_id or self._default_owner()

        now = self._rt.clock.now()
        tid, ref = self._rt.mint("TASK")
        task = Task(
            id=tid,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=requester.as_actor(),
            updated_at=now,
            organization_id=self._rt.organization().id,
            title=title or objective[:80],
            objective=objective,
            origin=TaskOrigin.HUMAN,
            requested_by=requester.id,
            owner_agent_id=owner_id,
            sponsor_agent_id=sponsor_agent_id
            or (requested_by_agent_id if risk_level >= RiskLevel.HIGH else None),
            risk_level=risk_level,
            constraints=constraints or Constraints(),
            required_outputs=required_outputs,
            max_plan_revisions=self._rt.policies.max_plan_revisions(),
        )
        task = self._rt.repos.tasks.insert(task)

        self._engine.record_creation(
            task,
            actor=requester.as_actor(),
            reason_code="HUMAN_REQUEST",
            reason=f"{requester.name} submitted objective: {objective}",
            correlation_id=task.id,
            event_name="qor.task.created",
            payload={"objective": objective, "risk_level": str(risk_level)},
        )

        result = self._engine.fire(
            task,
            "admit",
            actor=requester.as_actor(),
            reason_code="HUMAN_REQUEST",
            reason=f"admitted by {requester.name} as {risk_level} risk organizational task",
            correlation_id=task.id,
        )
        return self._rt.repos.tasks.save(result.entity)

    def open_from_outcome(self, outcome: Outcome, *, recorded_by_agent_id: UUID) -> Task:
        """Open one Follow-up Task through the normal create → admit door.

        The recorder is accountable. SYSTEM admits, because Follow-through is
        organizational governance rather than a Human submitting a new objective.
        """
        recorder = self._rt.repos.agents.get(recorded_by_agent_id)
        owner_id = self._default_owner()
        system = ActorRef.system("QOR-FOLLOW-THROUGH")
        now = self._rt.clock.now()
        tid, ref = self._rt.mint("TASK")
        objective = (
            f"Address the CONTRADICTED outcome on {outcome.subject_key} "
            f"({outcome.ref}). {outcome.statement}".strip()
        )
        task = Task(
            id=tid,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=system,
            updated_at=now,
            organization_id=self._rt.organization().id,
            title=f"Follow through: {outcome.subject_key}"[:80],
            objective=objective,
            origin=TaskOrigin.FOLLOW_THROUGH,
            requested_by=recorder.id,
            owner_agent_id=owner_id,
            parent_task_id=outcome.task_id,
            source_outcome_id=outcome.id,
            required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
            max_plan_revisions=self._rt.policies.max_plan_revisions(),
        )
        task = self._rt.repos.tasks.insert(task)

        self._engine.record_creation(
            task,
            actor=system,
            reason_code="FOLLOW_THROUGH_REQUIRED",
            reason=(
                f"organization opened follow-through for {outcome.ref} on {outcome.subject_key}"
            ),
            correlation_id=task.id,
            event_name="qor.task.created",
            payload={
                "objective": objective,
                "source_outcome_ref": outcome.ref,
                "origin": TaskOrigin.FOLLOW_THROUGH.value,
            },
        )

        result = self._engine.fire(
            task,
            "admit",
            actor=system,
            reason_code="FOLLOW_THROUGH_REQUIRED",
            reason=f"admitted follow-through for {outcome.ref}",
            correlation_id=task.id,
            metadata={"outcome_ref": outcome.ref},
        )
        return self._rt.repos.tasks.save(result.entity)

    def open_from_schedule(self, schedule: Schedule) -> Task:
        """Open one Task from a due Schedule through the normal create → admit door.

        SYSTEM admits. Scheduling is organizational time, not a Human submitting
        a new objective and not an agent starting work.
        """
        owner_id = self._default_owner()
        system = ActorRef.system("QOR-SCHEDULER")
        now = self._rt.clock.now()
        tid, ref = self._rt.mint("TASK")
        task = Task(
            id=tid,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=system,
            updated_at=now,
            organization_id=self._rt.organization().id,
            title=schedule.objective[:80],
            objective=schedule.objective,
            origin=TaskOrigin.SCHEDULE,
            owner_agent_id=owner_id,
            required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
            max_plan_revisions=self._rt.policies.max_plan_revisions(),
        )
        task = self._rt.repos.tasks.insert(task)
        self._engine.record_creation(
            task,
            actor=system,
            reason_code="SCHEDULE_DUE",
            reason=f"schedule {schedule.ref} opened {schedule.occurrence_key()}",
            correlation_id=schedule.id,
            event_name="qor.task.created",
            payload={
                "objective": schedule.objective,
                "origin": TaskOrigin.SCHEDULE.value,
                "schedule_ref": schedule.ref,
                "occurrence_key": schedule.occurrence_key(),
            },
        )
        result = self._engine.fire(
            task,
            "admit",
            actor=system,
            reason_code="SCHEDULE_DUE",
            reason=f"admitted schedule {schedule.ref}",
            correlation_id=schedule.id,
            metadata={"schedule_ref": schedule.ref},
        )
        return self._rt.repos.tasks.save(result.entity)

    def open_from_event(self, route: EventRoute, event: Event) -> Task:
        """Open one Task from an EventRoute. Only the Event Router uses this door.

        SYSTEM admits. The objective comes from the route. The event is the fact
        that matched, not a requester and not a second execution chain.
        """
        owner_id = self._default_owner()
        system = ActorRef.system("QOR-EVENT-ROUTER")
        now = self._rt.clock.now()
        tid, ref = self._rt.mint("TASK")
        task = Task(
            id=tid,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=system,
            updated_at=now,
            organization_id=self._rt.organization().id,
            title=route.objective[:80],
            objective=route.objective,
            origin=TaskOrigin.EVENT,
            owner_agent_id=owner_id,
            required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
            max_plan_revisions=self._rt.policies.max_plan_revisions(),
        )
        task = self._rt.repos.tasks.insert(task)
        self._engine.record_creation(
            task,
            actor=system,
            reason_code="EVENT_ROUTE_OPENED",
            reason=f"event route {route.ref} opened {event.id}",
            correlation_id=event.id,
            event_name="qor.task.created",
            payload={
                "objective": route.objective,
                "origin": TaskOrigin.EVENT.value,
                "event_id": str(event.id),
                "route_id": str(route.id),
                "route_ref": route.ref,
            },
        )
        result = self._engine.fire(
            task,
            "admit",
            actor=system,
            reason_code="EVENT_ROUTE_OPENED",
            reason=f"admitted event route {route.ref}",
            correlation_id=event.id,
            metadata={"route_ref": route.ref, "event_id": str(event.id)},
        )
        return self._rt.repos.tasks.save(result.entity)

    def _default_owner(self) -> UUID:
        key = self._rt.config.organization.get("default_task_owner")
        if key is None:
            raise ValueError("organization.yaml must declare default_task_owner")
        return self._rt.agent_index[key]
