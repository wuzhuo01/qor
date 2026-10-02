"""Delegation -- capability in, agent out.

The filter chain is data-driven (``policies.yaml``) and the scoring is weighted,
but the part that matters architecturally is what the input is: a set of capability
keys. There is no agent name anywhere in this module's logic, and no branch on one.

Two rejection reasons are deliberately kept apart:

* **CapabilityGap** -- nobody in the organization holds this capability. Permanent;
  the node is blocked and the organization has learned something about itself.
* **no one available** -- holders exist but are busy, offline or excluded.
  Temporary; retrying later is meaningful.

Reporting both as "delegation failed" would destroy the distinction between "we
cannot do this" and "we cannot do this right now".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from qor.domain.organization import Agent
from qor.domain.task import AgentSelection, Task, TaskAssignment, TaskNode
from qor.kernel.actor import ActorRef
from qor.kernel.errors import CapabilityGap, QORError
from qor.services.authorization import Authorizer
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["DelegationService", "NoAgentAvailable", "Selection"]


class NoAgentAvailable(QORError):
    """Capability holders exist, but none can take the work right now."""

    def __init__(
        self, capabilities: tuple[str, ...], rejected: tuple[tuple[str, str], ...]
    ) -> None:
        self.capabilities = capabilities
        self.rejected = rejected
        detail = "; ".join(f"{ref}: {why}" for ref, why in rejected)
        super().__init__(f"no agent available for {list(capabilities)} -- {detail}")


@dataclass(frozen=True)
class Selection:
    agent: Agent
    record: AgentSelection


class DelegationService:
    def __init__(self, runtime: Runtime, engine: TransitionEngine, authorizer: Authorizer) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer

    # ---- selection -----------------------------------------------------

    def select(
        self,
        *,
        required_capabilities: tuple[str, ...],
        required_permissions: tuple[str, ...] = (),
        exclude_agent_ids: tuple[UUID, ...] = (),
        purpose: str = "",
    ) -> Selection:
        """Run the filter chain, then score. Records why each candidate lost."""
        if not required_capabilities:
            raise QORError("delegation requires at least one capability")

        everyone = sorted(self._rt.repos.agents.all(), key=lambda a: a.ref)
        rejected: list[tuple[str, str]] = []

        # Filter 1: capability match. Must hold ALL required capabilities.
        holders: list[Agent] = []
        for agent in everyone:
            missing = [c for c in required_capabilities if not agent.holds(c)]
            if missing:
                rejected.append((agent.ref, f"lacks capability {missing}"))
            else:
                holders.append(agent)

        if not holders:
            raise CapabilityGap(
                list(required_capabilities),
                f"no agent in the organization holds all of {list(required_capabilities)}",
            )

        # Filters 2-5: permission, exclusion, state, capacity.
        eligible: list[Agent] = []
        for agent in holders:
            reason = self._reject_reason(agent, required_permissions, exclude_agent_ids)
            if reason:
                rejected.append((agent.ref, reason))
            else:
                eligible.append(agent)

        if not eligible:
            raise NoAgentAvailable(required_capabilities, tuple(rejected))

        scored = sorted(
            ((self._score(a, required_capabilities), a) for a in eligible),
            key=lambda pair: (-pair[0], pair[1].ref),
        )
        best_score, winner = scored[0]
        for score, loser in scored[1:]:
            rejected.append((loser.ref, f"lower score {score:.3f} vs {best_score:.3f}"))

        record = AgentSelection(
            required_capabilities=required_capabilities,
            candidates_considered=tuple(a.ref for a in holders),
            rejected=tuple(rejected),
            selected_agent_ref=winner.ref,
            score=round(best_score, 4),
            rationale=self._rationale(winner, required_capabilities, best_score, purpose),
        )
        return Selection(agent=winner, record=record)

    # ---- assignment ----------------------------------------------------

    def assign(self, task: Task, node: TaskNode) -> tuple[TaskNode, TaskAssignment]:
        """Move a node READY -> ASSIGNING -> ASSIGNED, creating the assignment.

        On CapabilityGap the node goes to BLOCKED with a recorded reason rather
        than raising into the caller: an organization that cannot do something
        should say so in its own log, not crash.
        """
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()

        # Rework: `rework` already moved the node REVIEWING -> RUNNING, so the node is
        # working again and only needs a fresh assignment for the new attempt. Driving
        # it back through READY/ASSIGNING would mean inventing a transition the
        # approved machine does not have.
        if node.status == "RUNNING":
            selection = self._match(node)
            return node, self._create_assignment(task, node, selection, actor)

        if node.status == "PENDING":
            node = self._rt.repos.nodes.save(
                self._engine.fire(
                    node,
                    "deps_met",
                    actor=actor,
                    reason_code="DEPENDENCIES_SATISFIED",
                    reason=f"upstream of {node.ref} is complete",
                    correlation_id=task.id,
                ).entity
            )

        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "start_assigning",
                actor=actor,
                reason_code="ROUTINE_PROGRESSION",
                reason=f"matching capabilities {list(node.required_capabilities)}",
                correlation_id=task.id,
            ).entity
        )

        try:
            selection = self._match(node)
        except (CapabilityGap, NoAgentAvailable) as exc:
            node = self._rt.repos.nodes.save(
                self._engine.fire(
                    node,
                    "capability_gap",
                    actor=actor,
                    reason_code="CAPABILITY_GAP",
                    reason=str(exc),
                    correlation_id=task.id,
                    payload={"required_capabilities": list(node.required_capabilities)},
                ).entity
            )
            raise

        assignment = self._create_assignment(task, node, selection, actor)
        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "assigned",
                actor=actor,
                reason_code="CAPABILITY_MATCHED",
                reason=selection.record.rationale,
                correlation_id=task.id,
            ).entity
        )
        return node, assignment

    def _match(self, node: TaskNode) -> Selection:
        others = self._assignees_on_other_nodes(node)
        purpose = f"execute {node.ref} '{node.name}' attempt {node.attempt}"
        try:
            return self.select(
                required_capabilities=node.required_capabilities,
                required_permissions=("artifact.create", "artifact.submit"),
                exclude_agent_ids=(*node.forbidden_agent_ids, *others),
                purpose=purpose,
            )
        except NoAgentAvailable:
            return self.select(
                required_capabilities=node.required_capabilities,
                required_permissions=("artifact.create", "artifact.submit"),
                exclude_agent_ids=node.forbidden_agent_ids,
                purpose=purpose,
            )

    def _assignees_on_other_nodes(self, node: TaskNode) -> tuple[UUID, ...]:
        seen: list[UUID] = []
        for assignment in self._rt.repos.assignments.all():
            if assignment.task_id != node.task_id or assignment.node_id == node.id:
                continue
            if assignment.assignee_agent_id not in seen:
                seen.append(assignment.assignee_agent_id)
        return tuple(seen)

    def _create_assignment(
        self, task: Task, node: TaskNode, selection: Selection, actor: ActorRef
    ) -> TaskAssignment:
        agent = selection.agent
        now = self._rt.clock.now()
        aid, ref = self._rt.mint("ASGN")

        # Creating an assignment grants an agent scoped authority, so it is itself an
        # exercise of authority and belongs in the audit chain. Without this, the log
        # would record every decision but not who was empowered to make them.
        self._authz.check(
            actor=actor,
            permission="assignment.create",
            target_type="TaskNode",
            target_id=node.id,
            target_ref=node.ref,
            correlation_id=task.id,
        )

        # Layer 3 of permission narrowing: grant only what this node needs.
        scope = frozenset({"artifact.create", "artifact.submit", "action.request.read_only"})
        granted = self._authz.effective_permissions(agent, assignment_scope=scope)

        assignment = TaskAssignment(
            id=aid,
            ref=ref,
            status="PROPOSED",
            created_at=now,
            created_by=actor,
            updated_at=now,
            task_id=task.id,
            node_id=node.id,
            assignee_agent_id=agent.id,
            assignee_role_id=agent.role_ids[0] if agent.role_ids else None,
            attempt=node.attempt,
            granted_permissions=granted,
            scope=scope,
            constraints=node.constraints,
            selection=selection.record,
            lease_expires_at=now + timedelta(seconds=self._rt.policies.lease_seconds()),
        )
        assignment = self._rt.repos.assignments.insert(assignment)

        self._engine.record_creation(
            assignment,
            actor=actor,
            reason_code="CAPABILITY_MATCHED",
            reason=selection.record.rationale,
            correlation_id=task.id,
            event_name="qor.delegation.agent_selected",
            payload={
                "node": node.ref,
                "required_capabilities": list(node.required_capabilities),
                "candidates_considered": list(selection.record.candidates_considered),
                "selected": agent.ref,
                "selected_name": agent.name,
                "score": selection.record.score,
                "rejected": [f"{r}: {why}" for r, why in selection.record.rejected],
            },
        )

        for transition, code, reason in (
            ("offer", "CAPABILITY_MATCHED", f"offered {node.ref} to {agent.name}"),
            ("accept", "ROUTINE_PROGRESSION", f"{agent.name} accepted {node.ref}"),
        ):
            assignment = self._rt.repos.assignments.save(
                self._engine.fire(
                    assignment,
                    transition,
                    actor=actor,
                    reason_code=code,
                    reason=reason,
                    correlation_id=task.id,
                ).entity
            )

        self._occupy(agent, task)
        return assignment

    def _occupy(self, agent: Agent, task: Task) -> None:
        """Record the occupancy, and flip to BUSY only if this fills the last slot.

        Order matters. The ``accept_assignment`` guard asks "did this agent have room
        to take the work", so it must be evaluated *before* the count increments.
        An agent with spare slots stays AVAILABLE, which is what lets a reviewer with
        three slots handle three concurrent reviews.
        """
        fresh = self._rt.repos.agents.get(agent.id)
        fills_last_slot = fresh.current_assignment_count + 1 >= fresh.max_concurrent_assignments

        if fills_last_slot and fresh.status == "AVAILABLE":
            fresh = self._engine.fire(
                fresh,
                "accept_assignment",
                actor=fresh.as_actor(),
                reason_code="AGENT_ASSIGNED_WORK",
                reason=(
                    f"{fresh.name} taking its last free slot "
                    f"({fresh.current_assignment_count + 1}/{fresh.max_concurrent_assignments})"
                ),
                correlation_id=task.id,
            ).entity

        fresh.current_assignment_count += 1
        self._rt.repos.agents.save(fresh)

    def release(self, agent_id: UUID, task: Task) -> None:
        agent = self._rt.repos.agents.get(agent_id)
        agent.current_assignment_count = max(0, agent.current_assignment_count - 1)
        agent = self._rt.repos.agents.save(agent)
        if agent.status == "BUSY" and agent.has_capacity:
            self._rt.repos.agents.save(
                self._engine.fire(
                    agent,
                    "release",
                    actor=agent.as_actor(),
                    reason_code="AGENT_WORK_RELEASED",
                    reason=f"{agent.name} has free capacity again",
                    correlation_id=task.id,
                ).entity
            )

    # ---- internals -----------------------------------------------------

    def _reject_reason(
        self,
        agent: Agent,
        required_permissions: tuple[str, ...],
        exclude_agent_ids: tuple[UUID, ...],
    ) -> str | None:
        for permission in required_permissions:
            allowed, basis = self._authz.probe(actor=agent.as_actor(), permission=permission)
            if not allowed:
                return f"permission filter: {basis}"

        if agent.id in exclude_agent_ids:
            return "excluded for this node (four-eyes or prior failure)"

        if agent.status not in ("AVAILABLE", "WAITING"):
            return f"state filter: agent is {agent.status}"

        if not agent.has_capacity:
            return (
                f"load filter: {agent.current_assignment_count}/"
                f"{agent.max_concurrent_assignments} assignments"
            )
        return None

    def _score(self, agent: Agent, capabilities: tuple[str, ...]) -> float:
        weights = self._rt.policies.delegation_weights()
        proficiencies = [
            cap.proficiency for cap in (agent.capability(k) for k in capabilities) if cap
        ]
        proficiency = sum(proficiencies) / len(proficiencies) if proficiencies else 0.0
        idle = 1.0 - (agent.current_assignment_count / max(1, agent.max_concurrent_assignments))
        return (
            weights.get("proficiency", 0.0) * proficiency
            + weights.get("trust_score", 0.0) * agent.trust_score
            + weights.get("idle_capacity", 0.0) * idle
        )

    def _rationale(
        self, agent: Agent, capabilities: tuple[str, ...], score: float, purpose: str
    ) -> str:
        parts = [
            f"selected {agent.name} ({agent.ref}) to {purpose}"
            if purpose
            else f"selected {agent.name}",
            f"holds {list(capabilities)}",
        ]
        detail = ", ".join(
            f"{k} proficiency {agent.capability(k).proficiency:.2f}"  # type: ignore[union-attr]
            for k in capabilities
            if agent.capability(k)
        )
        parts.append(detail)
        parts.append(f"trust {agent.trust_score:.2f}, score {score:.3f}")
        return "; ".join(p for p in parts if p)
