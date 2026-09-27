"""Orchestrator -- drives one Task through the organizational loop.

This module is deliberately thin and contains no organizational logic. It asks the
state machines what is possible and asks the services to do it. In particular it
has no knowledge of any agent, and it does not know how many nodes a plan has or
what they are for -- it walks whatever DAG the planner produced.

That is the actual test of "not a fixed workflow": swap the planner and this file
does not change.

One thing found while making a real DAG run, recorded as an Architecture Finding
rather than papered over: node-level review must happen *inside* the Task's RUNNING
state, because a downstream node cannot start until its upstream node is ACCEPTED,
and acceptance requires review. The Task's REVIEW state therefore acts as an
organizational gate confirming all node reviews resolved, not as the place where
reviewing happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from qor.domain.action import Action
from qor.domain.artifact import Artifact
from qor.domain.decision import Decision
from qor.domain.enums import ArtifactType, Environment, ResponsibilitySource, RiskLevel
from qor.domain.review import Review
from qor.domain.task import NodeKind, Task, TaskNode, TaskPlan
from qor.domain.values import Constraints
from qor.kernel.actor import ActorRef
from qor.kernel.errors import CapabilityGap, EntryRejected, QORError, TransitionRejected
from qor.ports.agent_runtime import AgentRuntimePort
from qor.ports.llm import LLMUnavailable
from qor.ports.planner import PlannerPort
from qor.ports.world import WorldReader, WorldWriter
from qor.services.action_gateway import ActionGateway
from qor.services.authorization import Authorizer
from qor.services.decision import DecisionService
from qor.services.delegation import DelegationService, NoAgentAvailable
from qor.services.event_router import EventRouter
from qor.services.execution import ExecutionService
from qor.services.intake import IntakeService
from qor.services.lessons import LessonService
from qor.services.outcomes import OutcomeService
from qor.services.planning import PlanningService, PlanRejected
from qor.services.responsibility import ResponsibilityService
from qor.services.review import ReviewService
from qor.services.runtime import Runtime
from qor.services.scheduler import SchedulerService
from qor.services.transitions import TransitionEngine

__all__ = ["Orchestrator", "RunResult"]


@dataclass
class RunResult:
    task: Task
    plan: TaskPlan | None = None
    artifacts: tuple[Artifact, ...] = ()
    reviews: tuple[Review, ...] = ()
    decision: Decision | None = None
    actions: tuple[Action, ...] = ()
    blocked_nodes: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.task.status == "COMPLETED"


class Orchestrator:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        planner: PlannerPort,
        agents: AgentRuntimePort,
        *,
        world_writer: WorldWriter | None = None,
        world_reader: WorldReader | None = None,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self.delegation = DelegationService(runtime, engine, authorizer)
        self.intake = IntakeService(runtime, engine, authorizer)
        self.planning = PlanningService(runtime, engine, authorizer, planner)
        self.execution = ExecutionService(runtime, engine, authorizer, agents, self.delegation)
        self.reviewing = ReviewService(runtime, engine, agents, self.delegation)
        self.gateway = ActionGateway(
            runtime, engine, authorizer, world_writer=world_writer, world_reader=world_reader
        )
        self.outcomes = OutcomeService(runtime, engine, authorizer, intake=self.intake)
        self.lessons = LessonService(runtime, engine, authorizer, planning=self.planning)
        self.scheduling = SchedulerService(runtime, engine, self.intake)
        self.routing = EventRouter(runtime, engine, self.intake)
        self.responsibilities = ResponsibilityService(runtime, engine)
        self.deciding = DecisionService(
            runtime,
            engine,
            authorizer,
            agents,
            outcomes=self.outcomes,
            world_reader=world_reader,
        )

    # ---- entry point ---------------------------------------------------

    def run_objective(
        self,
        objective: str,
        *,
        requested_by_agent_id: UUID | None = None,
        risk_level: RiskLevel = RiskLevel.ROUTINE,
        required_outputs: tuple[ArtifactType, ...] = (
            ArtifactType.SYNTHESIS,
            ArtifactType.RECOMMENDATION,
        ),
        constraints: Constraints | None = None,
    ) -> RunResult:
        """Submit an objective and run it to a terminal state.

        The requester defaults to whoever ``organization.yaml`` names as Human CIO.
        It is resolved from configuration rather than written here, because a service
        module containing an agent identifier is the first step toward a service
        module containing an agent workflow. ``tests/test_architecture.py`` enforces
        that no agent name or key literal appears in this layer.
        """
        requester = (
            requested_by_agent_id or self._rt.agent_index[self._rt.config.organization["human_cio"]]
        )
        task = self.intake.submit(
            objective=objective,
            requested_by_agent_id=requester,
            risk_level=risk_level,
            required_outputs=required_outputs,
            constraints=constraints,
        )
        return self.run_task(task)

    def run_task(self, task: Task) -> RunResult:
        """Drive a task from its current state to a terminal one.

        The entry check runs once, on the stored status at this call. Later
        states inside this same call are the execution lifecycle, not new entries.

        If the task is already READY with an active plan, execution proceeds
        without calling the planner. That is the T22 freeze: Matching may run
        again, planning must not.
        """
        task = self._rt.repos.tasks.get(task.id)
        self._require_execution_entry(task)
        result = RunResult(task=task)

        if task.status == "READY" and task.active_plan_id is not None:
            plan = self._rt.repos.plans.get(task.active_plan_id)
        else:
            try:
                task, plan = self.planning.plan(task)
            except (LLMUnavailable, PlanRejected) as exc:
                return self._fail_planning(task, result, exc)

        result.plan = plan

        owner_actor = self._owner_actor(task)
        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "start",
                actor=owner_actor,
                reason_code="ROUTINE_PROGRESSION",
                reason=f"beginning execution of {len(plan.node_ids)} nodes in {plan.ref}",
                correlation_id=task.id,
            ).entity
        )

        blocked = self._run_phase(task, plan, NodeKind.WORK, result)
        result.blocked_nodes = blocked
        if blocked:
            return self._finish(task, result, note=f"blocked nodes: {list(blocked)}")

        task = self._advance(
            task, "nodes_submitted", "WORK_SUBMITTED", "all evidence nodes submitted"
        )
        task = self._advance(
            task, "reviews_passed", "REVIEW_PASSED", "all required node reviews resolved"
        )

        blocked = self._run_phase(task, plan, NodeKind.SYNTHESIS, result)
        if blocked:
            result.blocked_nodes = blocked
            return self._finish(task, result, note=f"blocked synthesis nodes: {list(blocked)}")

        task = self.deciding.advance_task_to_deciding(self._rt.repos.tasks.get(task.id))
        task, decision, proposal = self.deciding.decide(task)
        result.decision = decision

        if proposal.proposed_actions:
            actions = self.gateway.request(task, decision, proposal.proposed_actions)
            result.actions = actions
            waiting = [action for action in actions if action.status == "APPROVAL_PENDING"]
            if waiting:
                task = self._rt.repos.tasks.save(
                    self._engine.fire(
                        task,
                        "request_human_approval",
                        actor=self._owner_actor(task),
                        reason_code="APPROVAL_REQUIRED_BY_POLICY",
                        reason=(
                            f"{len(waiting)} sandbox action(s) require a human grant "
                            f"before they can be approved"
                        ),
                        correlation_id=task.id,
                    ).entity
                )
                return self._finish(task, result)
            delivered: list[Action] = []
            for action in actions:
                if (
                    not action.dry_run
                    and action.target.environment is Environment.SANDBOX
                    and action.status == "APPROVED"
                ):
                    delivered.append(self.gateway.deliver(task, action))
                else:
                    delivered.append(action)
            actions = tuple(delivered)
            result.actions = actions
            task = self._advance(
                task,
                "actions_requested",
                "ACTIONS_REQUESTED",
                f"{len(actions)} action(s) requested via the Action Gateway",
            )
            try:
                task = self._advance(
                    task,
                    "actions_settled",
                    "ACTIONS_REACHED_STOP_STATE",
                    "all actions reached their policy stop state",
                )
            except TransitionRejected as exc:
                return self._finish(
                    self._rt.repos.tasks.get(task.id),
                    result,
                    note=f"actions did not reach policy stop state: {exc}",
                )
        else:
            task = self._advance(
                task,
                "decided_no_action",
                "DECISION_NO_ACTION",
                f"decision {decision.ref} requires no action",
            )

        return self._finish(task, result)

    def grant_approval(self, task_id: UUID, *, granted_by_agent_id: UUID) -> RunResult:
        task = self._rt.repos.tasks.get(task_id)
        granter = self._rt.repos.agents.get(granted_by_agent_id)
        actor = granter.as_actor()
        self._authz.check(
            actor=actor,
            permission="approval.grant",
            target_type="Task",
            target_id=task.id,
            target_ref=task.ref,
            correlation_id=task.id,
        )
        if task.status != "AWAITING_HUMAN":
            raise QORError(f"{task.ref} is {task.status}, not AWAITING_HUMAN")
        actions = tuple(
            action
            for action in sorted(self._rt.repos.actions.all(), key=lambda item: item.ref)
            if action.task_id == task.id
        )
        if not actions:
            raise QORError(f"{task.ref} has no Action to grant")
        granted: list[Action] = []
        for action in actions:
            if action.target.environment is not Environment.SANDBOX:
                raise QORError(f"{action.ref} is not a SANDBOX action")
            granted.append(self.gateway.grant(task, action, actor))
            self.responsibilities.complete_for_source(ResponsibilitySource.ACTION, action.id)
        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "human_granted",
                actor=actor,
                reason_code="APPROVAL_GRANTED",
                reason=f"{granter.name} granted sandbox approval",
                correlation_id=task.id,
            ).entity
        )
        self.responsibilities.complete_for_source(ResponsibilitySource.TASK, task.id)
        continued: list[Action] = []
        for action in granted:
            if (
                not action.dry_run
                and action.target.environment is Environment.SANDBOX
                and action.status == "APPROVED"
            ):
                continued.append(self.gateway.recheck_and_deliver(task, action))
            else:
                continued.append(action)
        result = RunResult(task=task, decision=self._decision_for(task), actions=tuple(continued))
        try:
            task = self._advance(
                task,
                "actions_settled",
                "ACTIONS_REACHED_STOP_STATE",
                "all actions reached their policy stop state after human grant",
            )
        except TransitionRejected as exc:
            return self._finish(
                self._rt.repos.tasks.get(task.id),
                result,
                note=f"actions did not reach policy stop state: {exc}",
            )
        result = RunResult(task=task, decision=self._decision_for(task), actions=tuple(continued))
        return self._finish(task, result)

    def deny_approval(self, task_id: UUID, *, denied_by_agent_id: UUID) -> RunResult:
        task = self._rt.repos.tasks.get(task_id)
        denier = self._rt.repos.agents.get(denied_by_agent_id)
        actor = denier.as_actor()
        self._authz.check(
            actor=actor,
            permission="approval.grant",
            target_type="Task",
            target_id=task.id,
            target_ref=task.ref,
            correlation_id=task.id,
        )
        if task.status != "AWAITING_HUMAN":
            raise QORError(f"{task.ref} is {task.status}, not AWAITING_HUMAN")
        actions = tuple(
            action
            for action in sorted(self._rt.repos.actions.all(), key=lambda item: item.ref)
            if action.task_id == task.id
        )
        if not actions:
            raise QORError(f"{task.ref} has no Action to deny")
        denied: list[Action] = []
        for action in actions:
            denied.append(self.gateway.deny(task, action, actor))
            self.responsibilities.complete_for_source(ResponsibilitySource.ACTION, action.id)
        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "human_denied",
                actor=actor,
                reason_code="APPROVAL_DENIED",
                reason=f"{denier.name} denied sandbox approval",
                correlation_id=task.id,
            ).entity
        )
        self.responsibilities.complete_for_source(ResponsibilitySource.TASK, task.id)
        result = RunResult(task=task, decision=self._decision_for(task), actions=tuple(denied))
        return self._finish(task, result)

    def escalate_task(self, task_id: UUID, *, escalated_by_agent_id: UUID) -> RunResult:
        task = self._rt.repos.tasks.get(task_id)
        if task.status != "AWAITING_HUMAN":
            raise QORError(f"{task.ref} is {task.status}, not AWAITING_HUMAN")
        actor_agent = self._rt.repos.agents.get(escalated_by_agent_id)
        actor = actor_agent.as_actor()
        self._authz.check(
            actor=actor,
            permission="task.escalate",
            target_type="Task",
            target_id=task.id,
            target_ref=task.ref,
            correlation_id=task.id,
        )
        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "escalate",
                actor=actor,
                reason_code="HUMAN_WAIT_ESCALATION",
                reason=f"{actor_agent.name} escalated a human-wait task",
                correlation_id=task.id,
            ).entity
        )
        return self._finish(task, RunResult(task=task, decision=self._decision_for(task)))

    def resolve_escalation(self, task_id: UUID, *, resolved_by_agent_id: UUID) -> RunResult:
        task = self._rt.repos.tasks.get(task_id)
        if task.status != "ESCALATED":
            raise QORError(f"{task.ref} is {task.status}, not ESCALATED")
        actor_agent = self._rt.repos.agents.get(resolved_by_agent_id)
        actor = actor_agent.as_actor()
        self._authz.check(
            actor=actor,
            permission="task.resolve_escalation",
            target_type="Task",
            target_id=task.id,
            target_ref=task.ref,
            correlation_id=task.id,
        )
        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "resolve_escalation",
                actor=actor,
                reason_code="ESCALATION_RESOLVED",
                reason=f"{actor_agent.name} returned the task to human wait",
                correlation_id=task.id,
            ).entity
        )
        if task.status != "AWAITING_HUMAN":
            raise QORError(f"{task.ref} resolved escalation to {task.status}, not AWAITING_HUMAN")
        return self._finish(task, RunResult(task=task, decision=self._decision_for(task)))

    def _decision_for(self, task: Task) -> Decision | None:
        matches = [
            decision for decision in self._rt.repos.decisions.all() if decision.task_id == task.id
        ]
        return matches[-1] if matches else None

    def _require_execution_entry(self, task: Task) -> None:
        """Entry Policy v0.2. Read and judge only. No writes."""
        if task.status == "OPEN":
            return
        if task.status == "READY" and task.active_plan_id is not None:
            return
        if task.status == "READY":
            raise EntryRejected(task.id, task.status, "INVALID_STATE")
        raise EntryRejected(task.id, task.status, "REJECT")

    # ---- phases --------------------------------------------------------

    def _run_phase(
        self, task: Task, plan: TaskPlan, kind: str, result: RunResult
    ) -> tuple[str, ...]:
        """Walk the DAG in topological order, running only nodes of one kind."""
        blocked: list[str] = []
        for node_id in plan.topological_order():
            node = self._rt.repos.nodes.get(node_id)
            if node.node_kind != kind or node.status in ("ACCEPTED", "SKIPPED"):
                continue
            try:
                self._run_node(task, node, result)
            except (CapabilityGap, NoAgentAvailable) as exc:
                result.notes.append(f"{node.ref}: {exc}")
                blocked.append(node.ref)
                self._block_task(task, str(exc), reason_code="CAPABILITY_GAP")
                break
        return tuple(blocked)

    def _run_node(self, task: Task, node: TaskNode, result: RunResult) -> TaskNode:
        """Assign, execute, review, and rework until accepted or out of attempts."""
        while True:
            node, assignment = self.delegation.assign(task, self._rt.repos.nodes.get(node.id))
            node, assignment, artifact = self.execution.execute(task, node, assignment)
            result.artifacts = (*result.artifacts, artifact)

            if not node.review_policy.required:
                node = self._rt.repos.nodes.save(
                    self._engine.fire(
                        node,
                        "accept_without_review",
                        actor=self._owner_actor(task),
                        reason_code="REVIEW_NOT_REQUIRED",
                        reason="node review policy does not require independent review",
                        correlation_id=task.id,
                    ).entity
                )
                self._accept_artifact_without_review(task, artifact)
                return node

            outcome = self.reviewing.review(task, node, artifact)
            result.reviews = (*result.reviews, outcome.review)

            if outcome.passed:
                return outcome.node

            node = self._rt.repos.nodes.get(node.id)
            if outcome.reworkable and node.attempts_available:
                node = self.reviewing.request_rework(task, node, outcome.review)
                result.notes.append(
                    f"{node.ref}: review {outcome.review.ref} round {outcome.review.round} FAILED, "
                    f"reworking as attempt {node.attempt}"
                )
                continue

            self.reviewing.escalate_exhausted(task, node, outcome.review)
            raise QORError(
                f"{node.ref} exhausted its attempts; review {outcome.review.ref} FAILED and "
                f"the organization escalated rather than accepting unreviewed work"
            )

    def _accept_artifact_without_review(self, task: Task, artifact: Artifact) -> None:
        self._rt.repos.artifacts.save(
            self._engine.fire(
                artifact,
                "accept",
                actor=self._owner_actor(task),
                reason_code="REVIEW_NOT_REQUIRED",
                reason="accepted without review per node policy",
                correlation_id=task.id,
            ).entity
        )

    # ---- helpers -------------------------------------------------------

    def _advance(self, task: Task, transition: str, reason_code: str, reason: str) -> Task:
        fresh = self._rt.repos.tasks.get(task.id)
        return self._rt.repos.tasks.save(
            self._engine.fire(
                fresh,
                transition,
                actor=self._owner_actor(fresh),
                reason_code=reason_code,
                reason=reason,
                correlation_id=fresh.id,
            ).entity
        )

    def _fail_planning(self, task: Task, result: RunResult, exc: Exception) -> RunResult:
        """Planner failure is an organizational event, not a crash and not a stub.

        The task must already be in a state from which `block` is legal. Intake
        leaves it OPEN; start_planning leaves it PLANNING. Both are allowed.
        """
        if isinstance(exc, LLMUnavailable):
            code, prefix = "PLANNER_UNAVAILABLE", "planner unavailable"
        else:
            code, prefix = "PLAN_REJECTED", "plan rejected"
        fresh = self._rt.repos.tasks.get(task.id)
        blocked = self._block_task(fresh, f"{prefix}: {exc}", reason_code=code)
        result.notes.append(str(exc))
        return self._finish(blocked, result, note=f"{prefix}: {exc}")

    def _block_task(self, task: Task, reason: str, *, reason_code: str = "CAPABILITY_GAP") -> Task:
        fresh = self._rt.repos.tasks.get(task.id)
        return self._rt.repos.tasks.save(
            self._engine.fire(
                fresh,
                "block",
                actor=self._owner_actor(fresh),
                reason_code=reason_code,
                reason=reason,
                correlation_id=fresh.id,
            ).entity
        )

    def _finish(self, task: Task, result: RunResult, note: str = "") -> RunResult:
        fresh = self._rt.repos.tasks.get(task.id)
        result.artifacts = tuple(
            a
            for a in sorted(self._rt.repos.artifacts.all(), key=lambda x: x.ref)
            if a.task_id == fresh.id
        )
        result.reviews = tuple(
            r
            for r in sorted(self._rt.repos.reviews.all(), key=lambda x: x.ref)
            if r.task_id == fresh.id
        )
        result.actions = tuple(
            a
            for a in sorted(self._rt.repos.actions.all(), key=lambda x: x.ref)
            if a.task_id == fresh.id
        )
        # Written after the collections are refreshed: the rationale quotes them.
        if fresh.status == "COMPLETED":
            fresh.completion_rationale = self._completion_rationale(fresh, result)
            fresh = self._rt.repos.tasks.save(fresh)
        result.task = fresh
        if note:
            result.notes.append(note)
        return result

    def _completion_rationale(self, task: Task, result: RunResult) -> str:
        """Answer log question 10: why is this considered complete?"""
        accepted = [a.ref for a in result.artifacts if a.status == "ACCEPTED"]
        passed = [f"{r.ref}({r.status},round {r.round})" for r in result.reviews]
        parts = [
            f"all required outputs accepted: {sorted(t.value for t in task.required_outputs)}",
            f"accepted artifacts {accepted}",
            f"reviews {passed}",
        ]
        if result.decision is not None:
            parts.append(f"decision {result.decision.ref} DECIDED: {result.decision.statement}")
        if result.actions:
            parts.append(
                "actions at policy stop state: "
                + ", ".join(f"{a.ref}={a.status}" for a in result.actions)
            )
        else:
            parts.append("no action required")
        return "; ".join(parts)

    def _owner_actor(self, task: Task) -> ActorRef:
        if task.owner_agent_id is None:
            raise QORError(f"{task.ref} has no owner, so no actor can be attributed")
        return self._rt.repos.agents.get(task.owner_agent_id).as_actor()
