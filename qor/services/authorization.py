"""Authorization -- the single place permissions are decided, and always audited.

Three-layer narrowing (proposal §5.21.1):

    union(roles.granted)  -  agent.denied  -  assignment.scope

Every decision, allow or deny, writes an AuditRecord. Auditing only denials would
leave the more interesting question unanswerable: "who was allowed to do this, and
on what basis?"
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.audit import AuditRecord
from qor.domain.organization import Agent
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import AuthorizationError, InvariantViolation
from qor.services.runtime import Runtime

__all__ = ["Authorizer"]


class Authorizer:
    def __init__(self, runtime: Runtime) -> None:
        self._rt = runtime

    # ---- startup checks -------------------------------------------------

    def verify_no_ai_holds_exclusive_permissions(self) -> None:
        """Invariant I26, checked at load time rather than trusted.

        A configuration mistake here would be silent and catastrophic: an AI agent
        holding ``capability.risk_class.modify`` could legally reclassify a
        REALITY_AFFECTING capability as READ_ONLY and route real trades around the
        Action Gateway without breaking a single rule.
        """
        exclusive = self._rt.config.human_cio_exclusive
        roles = self._rt.role_map()
        for agent in self._rt.repos.agents.all():
            if agent.kind is not ActorKind.AGENT:
                continue
            held = agent.effective_permissions(roles)
            overlap = sorted(held & exclusive)
            if overlap:
                raise InvariantViolation(
                    "I26",
                    f"AI agent '{agent.name}' holds HUMAN-CIO-exclusive permissions {overlap}",
                )

    def verify_permission_catalog(self) -> None:
        """Every permission a role grants must exist in the catalog.

        Typos in a role definition would otherwise create permissions that can
        never be checked -- and therefore never denied.
        """
        known = self._rt.config.known_permissions
        for role in self._rt.repos.roles.all():
            unknown = sorted(set(role.granted_permissions) - known)
            if unknown:
                raise InvariantViolation(
                    "I6", f"role '{role.name}' grants unknown permissions {unknown}"
                )

    # ---- runtime checks -------------------------------------------------

    def effective_permissions(
        self, agent: Agent, *, assignment_scope: frozenset[str] | None = None
    ) -> frozenset[str]:
        base = agent.effective_permissions(self._rt.role_map())
        if assignment_scope is None:
            return base
        return frozenset(p for p in base if self._matches_any(p, assignment_scope))

    def check(
        self,
        *,
        actor: ActorRef,
        permission: str,
        target_type: str,
        target_id: UUID | None = None,
        target_ref: str = "",
        assignment_scope: frozenset[str] | None = None,
        correlation_id: UUID | None = None,
    ) -> AuditRecord:
        """Decide, audit, and raise on denial.

        Raising rather than returning False is deliberate: a caller that forgets to
        check the return value of a permission check is a security bug waiting to
        happen, whereas a caller that forgets to catch an exception fails loudly.
        """
        allowed, basis, policy_ref = self._decide(actor, permission, assignment_scope)
        record = self._write_audit(
            actor=actor,
            permission=permission,
            target_type=target_type,
            target_id=target_id,
            target_ref=target_ref,
            allowed=allowed,
            policy_ref=policy_ref,
            basis=basis,
            correlation_id=correlation_id,
        )
        if not allowed:
            raise AuthorizationError(str(actor), permission, basis)
        return record

    def probe(
        self,
        *,
        actor: ActorRef,
        permission: str,
        assignment_scope: frozenset[str] | None = None,
    ) -> tuple[bool, str]:
        """Non-auditing, non-raising check, for delegation filtering.

        Delegation asks "which of these twelve agents could do this" -- writing an
        audit record per rejected candidate would drown the real decisions. The
        chosen candidate's authority is audited when the assignment is created.
        """
        allowed, basis, _ = self._decide(actor, permission, assignment_scope)
        return allowed, basis

    # ---- internals -----------------------------------------------------

    def _decide(
        self,
        actor: ActorRef,
        permission: str,
        assignment_scope: frozenset[str] | None,
    ) -> tuple[bool, str, str]:
        # An undeclared permission is refused before anything else. Without this, a
        # misspelled permission name in a guard or service would fall through the
        # SYSTEM branch below and become a silent grant -- the worst possible failure
        # mode, since it looks like the check ran.
        if permission not in self._rt.config.known_permissions:
            return (
                False,
                f"'{permission}' is not a declared permission",
                "POLICY-AUTHZ-V1#unknown_permission",
            )

        if actor.is_machine_authority:
            # SYSTEM and RULE drive lifecycle transitions but are not accountable
            # principals, so they can never hold governance authority. Approval is
            # named explicitly as well as via the exclusive list, because "a machine
            # may not approve" must not depend on the exclusive list staying complete.
            if permission.startswith("approval.") or permission in (
                self._rt.config.human_cio_exclusive
            ):
                return (
                    False,
                    f"{actor.kind} is not an accountable principal",
                    "POLICY-AUTHZ-V1#machine_authority",
                )
            return True, f"{actor.kind} lifecycle authority", "POLICY-AUTHZ-V1"

        if actor.id is None:
            return False, "actor has no identity", "POLICY-AUTHZ-V1"

        agent = self._rt.repos.agents.find(actor.id)
        if agent is None:
            return False, f"unknown agent {actor.id}", "POLICY-AUTHZ-V1"

        if permission in self._rt.config.human_cio_exclusive and agent.kind is not ActorKind.HUMAN:
            return (
                False,
                f"'{permission}' is HUMAN-CIO-exclusive; agent kind is {agent.kind}",
                "POLICY-AUTHZ-V1#human_cio_exclusive",
            )

        roles = self._rt.role_map()
        granted = agent.effective_permissions(roles)
        if not self._matches_any(permission, granted):
            denied_hit = self._matches_any(permission, agent.denied_permissions)
            reason = (
                f"'{permission}' explicitly denied on agent"
                if denied_hit
                else f"'{permission}' not granted by roles {[roles[r].name for r in agent.role_ids if r in roles]}"
            )
            return False, reason, "POLICY-AUTHZ-V1#three_layer"

        if assignment_scope is not None and not self._matches_any(permission, assignment_scope):
            return (
                False,
                f"'{permission}' outside assignment scope {sorted(assignment_scope)}",
                "POLICY-AUTHZ-V1#assignment_scope",
            )

        return (
            True,
            f"granted via roles {[roles[r].name for r in agent.role_ids if r in roles]}",
            "POLICY-AUTHZ-V1#three_layer",
        )

    @staticmethod
    def _matches_any(permission: str, held: frozenset[str] | set[str]) -> bool:
        """Exact match, or a held prefix ending in '.*'.

        Prefix grants let a role say "any read-only action request" without
        enumerating operations, while still being explicit that it is a wildcard.
        """
        if permission in held:
            return True
        return any(item.endswith(".*") and permission.startswith(item[:-1]) for item in held)

    def _write_audit(
        self,
        *,
        actor: ActorRef,
        permission: str,
        target_type: str,
        target_id: UUID | None,
        target_ref: str,
        allowed: bool,
        policy_ref: str,
        basis: str,
        correlation_id: UUID | None,
    ) -> AuditRecord:
        record = AuditRecord(
            seq=self._rt.log.next_seq(),
            actor=actor,
            permission=permission,
            target_type=target_type,
            target_id=target_id,
            target_ref=target_ref,
            allowed=allowed,
            policy_ref=policy_ref,
            basis=basis,
            occurred_at=self._rt.clock.now(),
            correlation_id=correlation_id,
            prev_hash=self._rt.log.last_audit_hash(),
        ).sealed()
        return self._rt.log.append_audit(record)
