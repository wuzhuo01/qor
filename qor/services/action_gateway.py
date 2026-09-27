"""Action Gateway -- the only exit to a world QOR does not own.

The gateway runs three checks in order and records each verdict on the Action:

    PERMISSION -> RISK -> APPROVAL -> APPROVED

Dry-run actions still stop there: ``execute`` carries ``action_is_not_dry_run``.
A SANDBOX action with ``dry_run=false`` may continue through ``deliver`` into a
controlled sandbox world, then settle only after an independent reread. LIVE
remains closed: execute requires ``action.execute.sandbox`` and a SANDBOX target.

One detail is worth reading closely. The PERMISSION check evaluates the **sandbox
tier** permission, because requesting a simulation is genuinely different authority
from requesting a live change. It also records whether the requester holds the
**live tier** permission, so the audit log states explicitly what would happen if
this action were promoted. In V0.1 the answer is "it would be denied", and that is
information the organization should have written down rather than discovered later.
"""

from __future__ import annotations

from qor.domain.action import Action, ActionCheck, ActionTarget, Verification
from qor.domain.decision import Decision
from qor.domain.enums import Environment, RiskClass
from qor.domain.task import Task
from qor.kernel.actor import ActorRef
from qor.kernel.errors import QORError
from qor.ports.agent_runtime import ActionProposal
from qor.ports.world import WorldReader, WorldWriter
from qor.services.authorization import Authorizer
from qor.services.config import ApprovalOutcome
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["ActionGateway"]

SANDBOX_TIER_PERMISSION = "action.request.simulation"
HUMAN_GRANT_POLICY = "POLICY-APPROVAL-V1#human_grant"


def _latest_check(action: Action, kind: str) -> ActionCheck | None:
    found = [item for item in action.checks if item.kind == kind]
    return found[-1] if found else None


class ActionGateway:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        *,
        world_writer: WorldWriter | None = None,
        world_reader: WorldReader | None = None,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._world_writer = world_writer
        self._world_reader = world_reader

    def request(
        self, task: Task, decision: Decision, proposals: tuple[ActionProposal, ...]
    ) -> tuple[Action, ...]:
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()
        created: list[Action] = []
        for index, proposal in enumerate(proposals, start=1):
            action = self._create(task, decision, proposal, actor, index)
            created.append(self._gate(task, action, actor))
        return tuple(created)

    # ---- creation ------------------------------------------------------

    def _create(
        self,
        task: Task,
        decision: Decision,
        proposal: ActionProposal,
        actor: ActorRef,
        index: int,
    ) -> Action:
        environment = Environment(proposal.environment)
        now = self._rt.clock.now()
        aid, ref = self._rt.mint("ACT")
        action = Action(
            id=aid,
            ref=ref,
            status="REQUESTED",
            created_at=now,
            created_by=actor,
            updated_at=now,
            task_id=task.id,
            decision_id=decision.id,
            requested_by_agent_id=task.owner_agent_id,
            requested_by_role_id=actor.role_id,
            target=ActionTarget(
                system=proposal.system,
                resource=proposal.resource,
                environment=environment,
                operation=proposal.operation,
            ),
            payload=dict(proposal.payload),
            capability_key=proposal.capability_key,
            dry_run=proposal.dry_run,
            is_reversible=proposal.is_reversible,
            idempotency_key=f"{task.ref}:{decision.ref}:{proposal.operation}:{proposal.resource}:{index}",
        )
        action = self._rt.repos.actions.insert(action)
        self._engine.record_creation(
            action,
            actor=actor,
            reason_code="ACTIONS_REQUESTED",
            reason=(
                f"decision {decision.ref} requires {proposal.operation} on {proposal.resource} "
                f"in {environment.value}; dry_run={action.dry_run}"
            ),
            correlation_id=task.id,
            event_name="qor.action.requested",
            payload={
                "decision": decision.ref,
                "operation": proposal.operation,
                "environment": environment.value,
                "dry_run": action.dry_run,
            },
        )
        return action

    # ---- gating --------------------------------------------------------

    def _gate(self, task: Task, action: Action, actor: ActorRef) -> Action:
        action = self._rt.repos.actions.save(
            self._engine.fire(
                action,
                "validate",
                actor=actor,
                reason_code="ROUTINE_PROGRESSION",
                reason=f"{action.ref} accepted for gating; entering permission check",
                correlation_id=task.id,
            ).entity
        )

        action = self._record_check(action, self._permission_check(task, action, actor))
        if action.check("PERMISSION").verdict == "FAILED":  # type: ignore[union-attr]
            return self._reject(task, action, actor, "permission_denied", "PERMISSION_DENIED")
        action = self._rt.repos.actions.save(
            self._engine.fire(
                action,
                "permission_passed",
                actor=actor,
                reason_code="PERMISSION_GRANTED",
                reason=action.check("PERMISSION").detail,  # type: ignore[union-attr]
                correlation_id=task.id,
            ).entity
        )

        action = self._record_check(action, self._risk_check(task, action))
        if action.check("RISK").verdict == "FAILED":  # type: ignore[union-attr]
            return self._block(task, action, actor)
        action = self._rt.repos.actions.save(
            self._engine.fire(
                action,
                "risk_passed",
                actor=actor,
                reason_code="RISK_CHECK_PASSED",
                reason=action.check("RISK").detail,  # type: ignore[union-attr]
                correlation_id=task.id,
            ).entity
        )

        outcome = self._approval_outcome(task, action)
        if not outcome.required:
            action = self._record_check(
                action,
                ActionCheck(
                    kind="APPROVAL",
                    verdict="WAIVED",
                    policy_ref=outcome.policy_ref,
                    detail=f"{outcome.basis}. {self._reduction_basis(action)}",
                    checked_at=self._rt.clock.now(),
                ),
            )
            approval = action.check("APPROVAL")
            action = self._rt.repos.actions.save(
                self._engine.fire(
                    action,
                    "approval_granted",
                    actor=actor,
                    reason_code="APPROVAL_WAIVED_BY_POLICY",
                    reason=approval.detail,  # type: ignore[union-attr]
                    correlation_id=task.id,
                ).entity
            )
            if action.dry_run:
                self._log_stop(task, action, actor)
            return action
        # Policy requires a Human GRANT. Do not record PASSED and do not self-approve.
        return action

    # ---- checks --------------------------------------------------------

    def _permission_check(self, task: Task, action: Action, actor: ActorRef) -> ActionCheck:
        live_permissions = (
            sorted(self._rt.capability(action.capability_key).required_permissions)
            if action.capability_key in self._rt.capability_index
            else []
        )

        if action.target.environment is Environment.SANDBOX:
            allowed, basis = self._authz.probe(actor=actor, permission=SANDBOX_TIER_PERMISSION)
            live_held = [
                p for p in live_permissions if self._authz.probe(actor=actor, permission=p)[0]
            ]
            missing_live = sorted(set(live_permissions) - set(live_held))
            detail = (
                f"sandbox tier '{SANDBOX_TIER_PERMISSION}': {basis}. "
                f"live tier {live_permissions or '[]'} "
                + (
                    f"NOT held ({missing_live}); promoting this action to LIVE would be denied"
                    if missing_live
                    else "held"
                )
            )
            self._authz.check(
                actor=actor,
                permission=SANDBOX_TIER_PERMISSION,
                target_type="Action",
                target_id=action.id,
                target_ref=action.ref,
                correlation_id=task.id,
            ) if allowed else None
            return ActionCheck(
                kind="PERMISSION",
                verdict="PASSED" if allowed else "FAILED",
                policy_ref="POLICY-AUTHZ-V1#action_tier",
                detail=detail,
                checked_at=self._rt.clock.now(),
            )

        missing = [
            p for p in live_permissions if not self._authz.probe(actor=actor, permission=p)[0]
        ]
        return ActionCheck(
            kind="PERMISSION",
            verdict="FAILED" if missing else "PASSED",
            policy_ref="POLICY-AUTHZ-V1#action_tier",
            detail=(
                f"live tier permissions missing: {missing}"
                if missing
                else f"live tier permissions held: {live_permissions}"
            ),
            checked_at=self._rt.clock.now(),
        )

    def _risk_check(self, task: Task, action: Action) -> ActionCheck:
        failures: list[str] = []
        notes: list[str] = []

        allowed_envs = self._rt.policies.allowed_environments(task.risk_level)
        if action.target.environment.value not in allowed_envs:
            failures.append(
                f"environment {action.target.environment.value} not permitted at task risk "
                f"{task.risk_level} (allowed {list(allowed_envs)})"
            )
        else:
            notes.append(f"environment {action.target.environment.value} permitted")

        org = self._rt.organization()
        if org.is_halted:
            failures.append("organization operating_mode is HALTED")
        else:
            notes.append(f"organization {org.operating_mode}, hard limits not engaged by a dry run")

        capability_class = (
            self._rt.capability(action.capability_key).risk_class
            if action.capability_key in self._rt.capability_index
            else RiskClass.READ_ONLY
        )
        if capability_class >= RiskClass.REALITY_AFFECTING and not action.dry_run:
            failures.append(
                f"capability risk class {capability_class} requires dry_run before execution"
            )
        else:
            notes.append(f"capability risk class {capability_class} with dry_run={action.dry_run}")

        if not action.is_reversible and self._rt.policies.requires_dry_run_when_irreversible():
            if not action.dry_run:
                failures.append("irreversible action must be dry-run first")
            else:
                notes.append("irreversible action is dry-run as required")

        return ActionCheck(
            kind="RISK",
            verdict="FAILED" if failures else "PASSED",
            policy_ref="POLICY-ACTION-V1#risk_checks",
            detail="; ".join(failures) if failures else "; ".join(notes),
            checked_at=self._rt.clock.now(),
        )

    def grant(self, task: Task, action: Action, actor: ActorRef) -> Action:
        if action.status != "APPROVAL_PENDING":
            raise QORError(f"{action.ref} is {action.status}, not waiting for approval")
        if action.target.environment is not Environment.SANDBOX:
            raise QORError(f"{action.ref} cannot be granted outside SANDBOX")
        action = self._record_check(
            action,
            ActionCheck(
                kind="APPROVAL",
                verdict="PASSED",
                policy_ref=HUMAN_GRANT_POLICY,
                detail=f"human grant by {actor.display}",
                checked_at=self._rt.clock.now(),
            ),
        )
        granted = self._rt.repos.actions.save(
            self._engine.fire(
                action,
                "approval_granted",
                actor=actor,
                reason_code="APPROVAL_GRANTED",
                reason=f"human grant by {actor.display}",
                correlation_id=task.id,
            ).entity
        )
        if granted.dry_run:
            self._log_stop(task, granted, actor)
        return granted

    def recheck_and_deliver(self, task: Task, action: Action) -> Action:
        """Re-run Permission and Risk after Human GRANT, then deliver if both pass.

        GRANT is a license to continue, not a snapshot of the earlier gate
        results and not a guarantee that the world will settle.
        """
        action = self._rt.repos.actions.get(action.id)
        if action.dry_run or action.status != "APPROVED":
            return action
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()
        action = self._record_check(action, self._permission_check(task, action, actor))
        permission = _latest_check(action, "PERMISSION")
        if permission is None or permission.verdict == "FAILED":
            return action
        action = self._record_check(action, self._risk_check(task, action))
        risk = _latest_check(action, "RISK")
        if risk is None or risk.verdict == "FAILED":
            return action
        return self.deliver(task, action)

    def deny(self, task: Task, action: Action, actor: ActorRef) -> Action:
        if action.status != "APPROVAL_PENDING":
            raise QORError(f"{action.ref} is {action.status}, not waiting for approval")
        action = self._record_check(
            action,
            ActionCheck(
                kind="APPROVAL",
                verdict="FAILED",
                policy_ref=HUMAN_GRANT_POLICY,
                detail=f"human deny by {actor.display}",
                checked_at=self._rt.clock.now(),
            ),
        )
        return self._reject(task, action, actor, "approval_denied", "APPROVAL_DENIED")

    def deliver(self, task: Task, action: Action) -> Action:
        """Execute an approved SANDBOX action against the world, then independently reread.

        SETTLED requires the reread to match the expected state declared from a
        reader call, never from the writer's return value. A second call on an
        already SETTLED action is a no-op so the world is not mutated twice.
        """
        action = self._rt.repos.actions.get(action.id)
        if action.status == "SETTLED":
            return action
        if action.dry_run or action.status != "APPROVED":
            return action
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()
        action = self._fire(
            task,
            action,
            actor,
            "execute",
            "ROUTINE_PROGRESSION",
            f"{action.ref} entering controlled sandbox execution",
        )
        action.execution_attempts += 1
        action = self._rt.repos.actions.save(action)

        resource = action.target.resource
        raw_delta = action.payload.get("delta", 1)
        delta = (
            int(raw_delta)
            if isinstance(raw_delta, int | str) and not isinstance(raw_delta, bool)
            else 1
        )
        before = self._world_reader.read(resource) if self._world_reader is not None else None
        expected_value = delta if before is None else int(before["value"]) + delta
        expected = {"value": str(expected_value)}

        try:
            if self._world_writer is None:
                raise QORError("sandbox world writer is not configured")
            self._world_writer.apply(
                resource=resource,
                delta=delta,
                idempotency_key=action.idempotency_key,
            )
            self._engine.emit(
                "qor.action.world_effect",
                entity=action,
                actor=actor,
                payload={
                    "resource": resource,
                    "delta": delta,
                    "idempotency_key": action.idempotency_key,
                },
                correlation_id=task.id,
            )
        except QORError as exc:
            self._engine.emit(
                "qor.action.world_effect_failed",
                entity=action,
                actor=actor,
                payload={"error": str(exc), "resource": resource},
                correlation_id=task.id,
            )

        action = self._fire(
            task,
            action,
            actor,
            "executed",
            "ROUTINE_PROGRESSION",
            f"{action.ref} execution attempt recorded; EXECUTED is not SETTLED",
        )
        action = self._fire(
            task,
            action,
            actor,
            "verify",
            "ROUTINE_PROGRESSION",
            f"{action.ref} independent sandbox world reread",
        )
        observed_raw = self._world_reader.read(resource) if self._world_reader is not None else None
        if observed_raw is None:
            observed: dict[str, str] = {}
            matched = False
        else:
            observed = {"value": str(observed_raw["value"])}
            matched = observed == expected
        action.verification = Verification(
            expected=expected,
            observed=observed,
            matched=matched,
            verified_at=self._rt.clock.now(),
        )
        action = self._rt.repos.actions.save(action)
        self._engine.emit(
            "qor.action.observed",
            entity=action,
            actor=actor,
            payload={
                "expected": expected,
                "observed": observed,
                "matched": matched,
            },
            correlation_id=task.id,
        )
        if matched:
            return self._fire(
                task,
                action,
                actor,
                "settle",
                "ACTIONS_REACHED_STOP_STATE",
                f"{action.ref} independent reread matched expected sandbox world state",
            )
        return self._fire(
            task,
            action,
            actor,
            "verification_failed",
            "UNRECOVERABLE_ERROR",
            f"{action.ref} independent reread did not match expected {expected}; observed {observed}",
        )

    def _fire(
        self,
        task: Task,
        action: Action,
        actor: ActorRef,
        transition: str,
        reason_code: str,
        reason: str,
    ) -> Action:
        return self._rt.repos.actions.save(
            self._engine.fire(
                action,
                transition,
                actor=actor,
                reason_code=reason_code,
                reason=reason,
                correlation_id=task.id,
            ).entity
        )

    def _approval_outcome(self, task: Task, action: Action) -> ApprovalOutcome:
        capability_class = (
            self._rt.capability(action.capability_key).risk_class
            if action.capability_key in self._rt.capability_index
            else RiskClass.READ_ONLY
        )
        effective_class, _ = self._rt.policies.effective_risk_class(
            nominal=capability_class,
            dry_run=action.dry_run,
            environment=action.target.environment,
        )
        return self._rt.policies.approval_outcome(
            environment=action.target.environment,
            task_risk=task.risk_level,
            irreversible=not action.is_reversible,
            capability_risk_class=effective_class,
        )

    def _reduction_basis(self, action: Action) -> str:
        capability_class = (
            self._rt.capability(action.capability_key).risk_class
            if action.capability_key in self._rt.capability_index
            else RiskClass.READ_ONLY
        )
        _, basis = self._rt.policies.effective_risk_class(
            nominal=capability_class,
            dry_run=action.dry_run,
            environment=action.target.environment,
        )
        return basis

    # ---- helpers -------------------------------------------------------

    def _record_check(self, action: Action, check: ActionCheck) -> Action:
        fresh = self._rt.repos.actions.get(action.id)
        fresh.checks = (*fresh.checks, check)
        return self._rt.repos.actions.save(fresh)

    def _reject(
        self, task: Task, action: Action, actor: ActorRef, transition: str, reason_code: str
    ) -> Action:
        kind = "PERMISSION" if transition == "permission_denied" else "APPROVAL"
        return self._rt.repos.actions.save(
            self._engine.fire(
                action,
                transition,
                actor=actor,
                reason_code=reason_code,
                reason=action.check(kind).detail,  # type: ignore[union-attr]
                correlation_id=task.id,
            ).entity
        )

    def _block(self, task: Task, action: Action, actor: ActorRef) -> Action:
        return self._rt.repos.actions.save(
            self._engine.fire(
                action,
                "risk_blocked",
                actor=actor,
                reason_code="RISK_LIMIT_BREACHED",
                reason=action.check("RISK").detail,  # type: ignore[union-attr]
                correlation_id=task.id,
            ).entity
        )

    def _log_stop(self, task: Task, action: Action, actor: ActorRef) -> None:
        """Record why the action stops here, using the real guard evaluation.

        Rather than asserting "we stopped because it is a dry run", this asks the
        state machine and writes down its answer, so the log reflects the mechanism
        instead of a comment about it.
        """
        can_execute, outcomes = self._engine.evaluate(action, "execute")
        blocking = "; ".join(f"{o.name}: {o.detail}" for o in outcomes if not o.passed)
        self._engine._emit(  # noqa: SLF001 - deliberate: this is a runtime fact, not a state change
            "qor.action.stopped_at_policy_stop_state",
            entity=action,
            actor=actor,
            payload={
                "status": action.status,
                "stop_state_key": action.stop_state_key,
                "policy_stop_state": self._rt.policies.dry_run_stop_state(),
                "execute_reachable": can_execute,
                "blocked_by": blocking,
                "checks": [f"{c.kind}={c.verdict}" for c in action.checks],
            },
            correlation_id=task.id,
            causation_id=None,
        )
