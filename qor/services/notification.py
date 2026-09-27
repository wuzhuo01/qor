"""Explicit ACTION_REQUIRED notifications.

The only trigger is create_action_required. Nothing in the task, responsibility,
schedule, or event-route lifecycle calls this service.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from qor.domain.audit import AuditRecord
from qor.domain.enums import (
    NotificationActionability,
    NotificationDeliveryState,
    ResponsibilitySource,
)
from qor.domain.notification import NotificationIntent
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import QORError
from qor.ports.notification import NotificationPort
from qor.services.runtime import Runtime

__all__ = ["NotificationService"]

_HUMAN_WAIT = {
    ResponsibilitySource.TASK: frozenset({"AWAITING_HUMAN", "ESCALATED"}),
    ResponsibilitySource.ACTION: frozenset({"APPROVAL_PENDING"}),
    ResponsibilitySource.TASK_NODE: frozenset({"ESCALATED"}),
}
_POLICY = "POLICY-NOTIFICATION-V1#action_required"
_ACTOR = ActorRef.system("QOR-NOTIFICATION")


class NotificationService:
    def __init__(self, runtime: Runtime, port: NotificationPort) -> None:
        self._rt = runtime
        self._port = port

    def create_action_required(
        self,
        source_type: ResponsibilitySource,
        source_id: UUID,
        reason: str,
        occurred_at: datetime | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
    ) -> NotificationIntent:
        if not reason.strip():
            raise QORError("notification requires a reason")
        active = self._rt.repos.responsibilities.find_active_by_source(source_type, source_id)
        if active is None:
            raise QORError(f"no ACTIVE responsibility for {source_type.value}:{source_id}")
        if active.source_type is not source_type or active.source_id != source_id:
            raise QORError("responsibility source does not match the notification source")
        human = self._rt.repos.agents.find(active.responsible_human_agent_id)
        if human is None:
            raise QORError("responsibility target agent does not exist")
        if human.kind is not ActorKind.HUMAN:
            raise QORError("responsibility target must be a HUMAN agent")
        status = self._source_status(source_type, source_id)
        if status not in _HUMAN_WAIT[source_type]:
            raise QORError(
                f"{source_type.value} {source_id} is {status}, "
                "which cannot take an ACTION_REQUIRED notification"
            )
        key = f"{active.id}:{NotificationActionability.ACTION_REQUIRED.value}"
        existing = self._rt.notifications.find_by_key(key)
        if existing is not None:
            return existing
        intent = NotificationIntent(
            id=self._rt.mint("NTF")[0],
            notification_key=key,
            responsibility_id=active.id,
            target_human_agent_id=human.id,
            source_type=source_type,
            source_id=source_id,
            source_status=status,
            actionability=NotificationActionability.ACTION_REQUIRED,
            reason=reason.strip(),
            occurred_at=occurred_at or self._rt.clock.now(),
            correlation_id=correlation_id,
            causation_id=causation_id,
            state=NotificationDeliveryState.NOT_ATTEMPTED,
        )
        intent = self._rt.notifications.save(intent)
        self._audit(intent)
        try:
            result = self._port.deliver(intent)
            outcome = (
                NotificationDeliveryState.DELIVERED
                if result.ok
                else NotificationDeliveryState.FAILED
            )
        except Exception:
            outcome = NotificationDeliveryState.FAILED
        return self._rt.notifications.save(intent.model_copy(update={"state": outcome}))

    def _source_status(self, source_type: ResponsibilitySource, source_id: UUID) -> str:
        if source_type is ResponsibilitySource.TASK:
            return self._rt.repos.tasks.get(source_id).status
        if source_type is ResponsibilitySource.ACTION:
            return self._rt.repos.actions.get(source_id).status
        if source_type is ResponsibilitySource.TASK_NODE:
            return self._rt.repos.nodes.get(source_id).status
        raise QORError(f"notification source {source_type} is not implemented")

    def _audit(self, intent: NotificationIntent) -> None:
        basis = (
            "NOTIFICATION_CREATED "
            f"notification={intent.id} "
            f"responsibility={intent.responsibility_id} "
            f"source={intent.source_type.value}:{intent.source_id} "
            f"human={intent.target_human_agent_id}"
        )
        record = AuditRecord(
            seq=self._rt.log.next_seq(),
            actor=_ACTOR,
            permission="notification.intent",
            target_type="NotificationIntent",
            target_id=intent.id,
            target_ref=intent.notification_key,
            allowed=True,
            policy_ref=_POLICY,
            basis=basis,
            occurred_at=self._rt.clock.now(),
            correlation_id=intent.correlation_id,
            prev_hash=self._rt.log.last_audit_hash(),
        ).sealed()
        self._rt.log.append_audit(record)
