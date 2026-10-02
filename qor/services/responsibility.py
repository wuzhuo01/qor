"""Responsibility binding.

Human CIO identity assigns a Human Agent to one existing human-work fact.
Entering AWAITING_HUMAN, APPROVAL_PENDING, or ESCALATED does not assign anyone.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.audit import AuditRecord
from qor.domain.enums import ResponsibilitySource
from qor.domain.organization import Agent
from qor.domain.responsibility import Responsibility
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import QORError
from qor.services.notification import NotificationService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["ResponsibilityService"]

_TASK_BINDABLE = frozenset({"AWAITING_HUMAN", "ESCALATED"})
_TASK_FINISHED = frozenset({"ACTION_PENDING", "DECIDING", "COMPLETED"})
_ACTION_FINISHED = frozenset(
    {"APPROVED", "REJECTED", "EXECUTING", "EXECUTED", "VERIFYING", "SETTLED", "COMPENSATED"}
)
_NODE_FINISHED = frozenset({"ACCEPTED", "SKIPPED"})
_POLICY = "POLICY-RESPONSIBILITY-V1#human_cio_identity"


class ResponsibilityService:
    def __init__(self, runtime: Runtime, engine: TransitionEngine) -> None:
        self._rt = runtime
        self._engine = engine
        self._notifications: NotificationService | None = None

    def attach_notification_service(self, notifications: NotificationService) -> None:
        """Wire the already-proven notification service. Orchestrator does not call it."""
        self._notifications = notifications

    def create(
        self,
        *,
        caller_agent_id: UUID,
        source_type: ResponsibilitySource,
        source_id: UUID,
        responsible_human_agent_id: UUID,
    ) -> Responsibility:
        cio = self._require_cio(caller_agent_id)
        self._require_bindable(source_type, source_id)
        target = self._require_human_target(responsible_human_agent_id)
        existing = self._rt.repos.responsibilities.find_active_by_source(source_type, source_id)
        if existing is not None:
            if existing.responsible_human_agent_id == target.id:
                return existing
            raise QORError(
                f"source {source_type.value}:{source_id} already has ACTIVE responsibility "
                f"{existing.ref}"
            )
        now = self._rt.clock.now()
        rid, ref = self._rt.mint("RESP")
        actor = cio.as_actor()
        created = Responsibility(
            id=rid,
            ref=ref,
            status="ACTIVE",
            created_at=now,
            created_by=actor,
            updated_at=now,
            source_type=source_type,
            source_id=source_id,
            responsible_human_agent_id=target.id,
            created_by_agent_id=cio.id,
        )
        created = self._rt.repos.responsibilities.insert(created)
        assignment_reason = f"{cio.ref} assigned {target.ref} to {source_type.value}:{source_id}"
        self._engine.record_creation(
            created,
            actor=actor,
            reason_code="RESPONSIBILITY_ASSIGNED",
            reason=assignment_reason,
            correlation_id=source_id,
            event_name="qor.responsibility.assigned",
            payload=self._payload(created),
        )
        self._audit("CREATE", actor, created, reason="")
        self._notify_new_active(created, assignment_reason)
        return created

    def transfer_responsibility(
        self,
        responsibility_id: UUID,
        *,
        caller_agent_id: UUID,
        responsible_human_agent_id: UUID,
        reason: str,
    ) -> Responsibility:
        cio = self._require_cio(caller_agent_id)
        if not reason.strip():
            raise QORError("responsibility transfer requires a reason")
        current = self._require_active(responsibility_id)
        target = self._require_human_target(responsible_human_agent_id)
        if target.id == current.responsible_human_agent_id:
            raise QORError("responsibility transfer requires a different human")
        actor = cio.as_actor()
        now = self._rt.clock.now()
        transferred = self._rt.repos.responsibilities.save(
            self._engine.fire(
                current,
                "transfer",
                actor=actor,
                reason_code="RESPONSIBILITY_TRANSFERRED",
                reason=reason.strip(),
                correlation_id=current.source_id,
                payload=self._payload(current, reason=reason.strip()),
            ).entity
        )
        rid, ref = self._rt.mint("RESP")
        successor = Responsibility(
            id=rid,
            ref=ref,
            status="ACTIVE",
            created_at=now,
            created_by=actor,
            updated_at=now,
            source_type=transferred.source_type,
            source_id=transferred.source_id,
            responsible_human_agent_id=target.id,
            created_by_agent_id=cio.id,
            transferred_from_responsibility_id=transferred.id,
            transfer_reason=reason.strip(),
        )
        successor = self._rt.repos.responsibilities.insert(successor)
        self._engine.record_creation(
            successor,
            actor=actor,
            reason_code="RESPONSIBILITY_TRANSFERRED",
            reason=reason.strip(),
            correlation_id=successor.source_id,
            event_name="qor.responsibility.assigned",
            payload=self._payload(successor, reason=reason.strip()),
        )
        self._audit("TRANSFER", actor, successor, reason=reason.strip())
        self._notify_new_active(successor, reason.strip())
        return successor

    def clear_responsibility(
        self,
        responsibility_id: UUID,
        *,
        caller_agent_id: UUID,
        reason: str,
    ) -> Responsibility:
        cio = self._require_cio(caller_agent_id)
        if not reason.strip():
            raise QORError("responsibility clear requires a reason")
        current = self._require_active(responsibility_id)
        now = self._rt.clock.now()
        prepared = current.model_copy(
            update={
                "clear_reason": reason.strip(),
                "cleared_at": now,
                "cleared_by_agent_id": cio.id,
            }
        )
        actor = cio.as_actor()
        cleared = self._rt.repos.responsibilities.save(
            self._engine.fire(
                prepared,
                "clear",
                actor=actor,
                reason_code="RESPONSIBILITY_CLEARED",
                reason=reason.strip(),
                correlation_id=current.source_id,
                payload=self._payload(prepared, reason=reason.strip()),
            ).entity
        )
        self._audit("CLEAR", actor, cleared, reason=reason.strip())
        return cleared

    def complete_for_source(
        self, source_type: ResponsibilitySource, source_id: UUID
    ) -> Responsibility | None:
        """End an assignment because the source finished its human work.

        No-op when nothing is active. Does not choose a human and does not
        create, transfer, or clear.
        """
        current = self._rt.repos.responsibilities.find_active_by_source(source_type, source_id)
        if current is None:
            return None
        if not self._human_work_finished(source_type, source_id):
            raise QORError(
                f"source {source_type.value}:{source_id} has not finished its human work"
            )
        now = self._rt.clock.now()
        prepared = current.model_copy(update={"completed_at": now})
        actor = ActorRef.system("QOR-RESPONSIBILITY")
        completed = self._rt.repos.responsibilities.save(
            self._engine.fire(
                prepared,
                "complete",
                actor=actor,
                reason_code="RESPONSIBILITY_COMPLETED",
                reason="source human work finished",
                correlation_id=source_id,
                payload=self._payload(prepared, reason="source human work finished"),
            ).entity
        )
        self._audit("COMPLETE", actor, completed, reason="source human work finished")
        return completed

    def _notify_new_active(self, item: Responsibility, reason: str) -> None:
        """Notify after a new ACTIVE row is stored. Refusal does not undo that row."""
        if self._notifications is None:
            raise QORError("notification service is not attached")
        try:
            self._notifications.create_action_required(item.source_type, item.source_id, reason)
        except QORError:
            return

    # ---- checks --------------------------------------------------------

    def _require_cio(self, caller_agent_id: UUID) -> Agent:
        org = self._rt.organization()
        if org.human_cio_agent_id is None:
            raise QORError("organization has no human CIO identity")
        cio = self._rt.repos.agents.find(org.human_cio_agent_id)
        if cio is None:
            raise QORError("human CIO agent does not exist")
        if cio.kind is not ActorKind.HUMAN:
            raise QORError("human CIO identity is not a HUMAN agent")
        if caller_agent_id != org.human_cio_agent_id:
            raise QORError("only the organization human CIO may assign responsibility")
        return cio

    def _require_human_target(self, agent_id: UUID) -> Agent:
        agent = self._rt.repos.agents.find(agent_id)
        if agent is None:
            raise QORError("responsibility target agent does not exist")
        if agent.kind is not ActorKind.HUMAN:
            raise QORError("responsibility target must be a HUMAN agent")
        return agent

    def _require_active(self, responsibility_id: UUID) -> Responsibility:
        found = self._rt.repos.responsibilities.get(responsibility_id)
        if found.status != "ACTIVE":
            raise QORError(f"{found.ref} is {found.status}, not ACTIVE")
        return found

    def _require_bindable(self, source_type: ResponsibilitySource, source_id: UUID) -> None:
        status = self._source_status(source_type, source_id)
        allowed = {
            ResponsibilitySource.TASK: _TASK_BINDABLE,
            ResponsibilitySource.ACTION: frozenset({"APPROVAL_PENDING"}),
            ResponsibilitySource.TASK_NODE: frozenset({"ESCALATED"}),
        }[source_type]
        if status not in allowed:
            raise QORError(
                f"{source_type.value} {source_id} is {status}, which cannot take a responsibility"
            )

    def _human_work_finished(self, source_type: ResponsibilitySource, source_id: UUID) -> bool:
        status = self._source_status(source_type, source_id)
        if source_type is ResponsibilitySource.TASK:
            return status in _TASK_FINISHED
        if source_type is ResponsibilitySource.ACTION:
            return status in _ACTION_FINISHED
        return status in _NODE_FINISHED

    def _source_status(self, source_type: ResponsibilitySource, source_id: UUID) -> str:
        if source_type is ResponsibilitySource.TASK:
            return self._rt.repos.tasks.get(source_id).status
        if source_type is ResponsibilitySource.ACTION:
            return self._rt.repos.actions.get(source_id).status
        if source_type is ResponsibilitySource.TASK_NODE:
            return self._rt.repos.nodes.get(source_id).status
        raise QORError(f"responsibility source {source_type} is not implemented")

    def _payload(self, item: Responsibility, *, reason: str = "") -> dict[str, str]:
        return {
            "responsibility_id": str(item.id),
            "source_type": item.source_type.value,
            "source_id": str(item.source_id),
            "responsible_human_agent_id": str(item.responsible_human_agent_id),
            "reason": reason,
        }

    def _audit(self, operation: str, actor: ActorRef, item: Responsibility, *, reason: str) -> None:
        basis = (
            f"{operation} responsibility={item.ref} source={item.source_type.value}:{item.source_id} "
            f"human={item.responsible_human_agent_id} reason={reason}"
        )
        record = AuditRecord(
            seq=self._rt.log.next_seq(),
            actor=actor,
            permission="organization.human_cio",
            target_type="Responsibility",
            target_id=item.id,
            target_ref=item.ref,
            allowed=True,
            policy_ref=_POLICY,
            basis=basis,
            occurred_at=self._rt.clock.now(),
            correlation_id=item.source_id,
            prev_hash=self._rt.log.last_audit_hash(),
        ).sealed()
        self._rt.log.append_audit(record)
