"""Guard registry.

Every guard named in a state machine YAML resolves to a function here. A missing
guard is a hard error at registry construction, not a silent pass -- a transition
whose precondition quietly evaporates is worse than one that never fires.

Guards are pure predicates over persisted state. They do not mutate anything, and
their results are recorded on the StateTransition so that "what was checked" is
answerable later.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from qor.domain.action import Action
from qor.domain.artifact import Artifact
from qor.domain.decision import Decision
from qor.domain.enums import ArtifactType, Environment, RiskClass, RiskLevel
from qor.domain.organization import Agent
from qor.domain.review import Review
from qor.domain.task import NodeKind, Task, TaskAssignment, TaskNode, TaskPlan
from qor.kernel.actor import ActorKind
from qor.kernel.errors import QORError
from qor.kernel.statemachine import GuardOutcome
from qor.services.runtime import Runtime

__all__ = ["GuardContext", "GuardRegistry"]

WORK_SUBMITTED_STATES = ("SUBMITTED", "REVIEWING", "ACCEPTED", "SKIPPED")
NODE_DONE_STATES = ("ACCEPTED", "SKIPPED")


@dataclass
class GuardContext:
    runtime: Runtime
    entity: Any
    extra: dict[str, Any] = field(default_factory=dict)

    # ---- convenience lookups -------------------------------------------

    @property
    def rt(self) -> Runtime:
        return self.runtime

    def task(self) -> Task:
        entity = self.entity
        if isinstance(entity, Task):
            return entity
        task_id = getattr(entity, "task_id", None)
        if task_id is None:
            raise QORError(f"{type(entity).__name__} has no task_id")
        return self.rt.repos.tasks.get(task_id)

    def active_plan(self) -> TaskPlan | None:
        task = self.task()
        if task.active_plan_id is None:
            return None
        return self.rt.repos.plans.find(task.active_plan_id)

    def plan_nodes(self, plan: TaskPlan) -> tuple[TaskNode, ...]:
        return tuple(self.rt.repos.nodes.get(nid) for nid in plan.node_ids)

    def nodes_of_kind(self, kind: str) -> tuple[TaskNode, ...]:
        plan = self.active_plan()
        if plan is None:
            return ()
        return tuple(n for n in self.plan_nodes(plan) if n.node_kind == kind)

    def reviews_for_artifact(self, artifact_id: UUID) -> tuple[Review, ...]:
        return tuple(r for r in self.rt.repos.reviews.all() if r.artifact_id == artifact_id)

    def reviews_for_node(self, node_id: UUID) -> tuple[Review, ...]:
        return tuple(r for r in self.rt.repos.reviews.all() if r.node_id == node_id)

    def artifacts_for_task(self, task_id: UUID) -> tuple[Artifact, ...]:
        return tuple(a for a in self.rt.repos.artifacts.all() if a.task_id == task_id)

    def assignments_for_node(self, node_id: UUID) -> tuple[TaskAssignment, ...]:
        return tuple(a for a in self.rt.repos.assignments.all() if a.node_id == node_id)

    def actions_for_task(self, task_id: UUID) -> tuple[Action, ...]:
        return tuple(a for a in self.rt.repos.actions.all() if a.task_id == task_id)

    def decisions_for_task(self, task_id: UUID) -> tuple[Decision, ...]:
        return tuple(d for d in self.rt.repos.decisions.all() if d.task_id == task_id)


GuardFn = Callable[[GuardContext], tuple[bool, str]]
_REGISTRY: dict[str, GuardFn] = {}


def guard(name: str) -> Callable[[GuardFn], GuardFn]:
    def wrap(fn: GuardFn) -> GuardFn:
        if name in _REGISTRY:
            raise ValueError(f"guard '{name}' already registered")
        _REGISTRY[name] = fn
        return fn

    return wrap


class GuardRegistry:
    def __init__(self, fns: dict[str, GuardFn]) -> None:
        self._fns = fns

    @classmethod
    def default(cls) -> GuardRegistry:
        return cls(dict(_REGISTRY))

    def names(self) -> frozenset[str]:
        return frozenset(self._fns)

    def run(self, name: str, ctx: GuardContext) -> GuardOutcome:
        fn = self._fns.get(name)
        if fn is None:
            raise QORError(f"guard '{name}' is referenced by a state machine but not implemented")
        passed, detail = fn(ctx)
        return GuardOutcome(name=name, passed=passed, detail=detail)


# =====================================================================
# Task guards
# =====================================================================


@guard("task_objective_present")
def _task_objective_present(ctx: GuardContext) -> tuple[bool, str]:
    task: Task = ctx.entity
    ok = bool(task.objective.strip())
    return ok, "objective present" if ok else "objective is empty"


@guard("task_risk_level_set")
def _task_risk_level_set(ctx: GuardContext) -> tuple[bool, str]:
    task: Task = ctx.entity
    return True, f"risk_level={task.risk_level}"


@guard("task_sponsor_present_if_capital")
def _task_sponsor_present_if_capital(ctx: GuardContext) -> tuple[bool, str]:
    """A task that could move capital needs a named human sponsor.

    Below HIGH there is nothing to sponsor, so this passes trivially rather than
    forcing ceremony onto routine work.
    """
    task: Task = ctx.entity
    if task.risk_level < RiskLevel.HIGH:
        return True, f"risk_level={task.risk_level} below HIGH, sponsor not required"
    if task.sponsor_agent_id is None:
        return False, f"risk_level={task.risk_level} requires a sponsor_agent_id"
    sponsor = ctx.rt.repos.agents.find(task.sponsor_agent_id)
    if sponsor is None or sponsor.kind is not ActorKind.HUMAN:
        return False, "sponsor must be an agent with kind=HUMAN"
    return True, f"sponsored by {sponsor.name}"


@guard("task_owner_present")
def _task_owner_present(ctx: GuardContext) -> tuple[bool, str]:
    task: Task = ctx.entity
    if task.owner_agent_id is None:
        return False, "task has no owner_agent_id"
    owner = ctx.rt.repos.agents.find(task.owner_agent_id)
    if owner is None:
        return False, f"owner {task.owner_agent_id} not found"
    return True, f"owner={owner.name}"


@guard("task_has_active_plan")
def _task_has_active_plan(ctx: GuardContext) -> tuple[bool, str]:
    plan = ctx.active_plan()
    if plan is None:
        return False, "task has no active_plan_id"
    if plan.status != "ACTIVE":
        return False, f"plan {plan.ref} is {plan.status}, not ACTIVE"
    return True, f"plan {plan.ref} active with {len(plan.node_ids)} nodes"


@guard("plan_covers_required_outputs")
def _plan_covers_required_outputs(ctx: GuardContext) -> tuple[bool, str]:
    """Every artifact type the task demands must be produced by some node."""
    if not ctx.rt.policies.enforce_output_coverage():
        return True, "output coverage not enforced by policy"
    task = ctx.task()
    plan = ctx.entity if isinstance(ctx.entity, TaskPlan) else ctx.active_plan()
    if plan is None:
        return False, "no plan to check"
    produced = {n.expected_artifact_type for n in ctx.plan_nodes(plan)}
    missing = sorted(t.value for t in task.required_outputs if t not in produced)
    if missing:
        return False, f"plan produces {sorted(t.value for t in produced)}, missing {missing}"
    return True, f"plan covers required outputs {sorted(t.value for t in task.required_outputs)}"


@guard("task_has_ready_node")
def _task_has_ready_node(ctx: GuardContext) -> tuple[bool, str]:
    plan = ctx.active_plan()
    if plan is None:
        return False, "no active plan"
    entries = plan.entry_nodes()
    if not entries:
        return False, "plan has no entry node"
    nodes = {n.id: n for n in ctx.plan_nodes(plan)}
    startable = [nodes[nid].ref for nid in entries if nodes[nid].status in ("PENDING", "READY")]
    if not startable:
        return False, "no entry node is PENDING or READY"
    return True, f"startable entry nodes: {startable}"


@guard("all_work_nodes_settled")
def _all_work_nodes_settled(ctx: GuardContext) -> tuple[bool, str]:
    """Every evidence-producing node has at least been submitted."""
    work = ctx.nodes_of_kind(NodeKind.WORK)
    if not work:
        return False, "plan has no WORK nodes"
    pending = [n.ref for n in work if n.status not in WORK_SUBMITTED_STATES]
    if pending:
        return False, f"WORK nodes not yet submitted: {pending}"
    return True, f"all {len(work)} WORK nodes submitted or settled"


@guard("all_required_reviews_resolved")
def _all_required_reviews_resolved(ctx: GuardContext) -> tuple[bool, str]:
    work = ctx.nodes_of_kind(NodeKind.WORK)
    unresolved = [f"{n.ref}={n.status}" for n in work if n.status not in NODE_DONE_STATES]
    if unresolved:
        return False, f"WORK nodes not accepted: {unresolved}"
    return True, f"all {len(work)} WORK nodes accepted or skipped"


@guard("has_reworkable_node")
def _has_reworkable_node(ctx: GuardContext) -> tuple[bool, str]:
    candidates: list[str] = []
    for node in ctx.nodes_of_kind(NodeKind.WORK) + ctx.nodes_of_kind(NodeKind.SYNTHESIS):
        if node.status != "REVIEWING":
            continue
        reviews = ctx.reviews_for_node(node.id)
        if any(r.is_reworkable for r in reviews) and node.attempts_available:
            candidates.append(node.ref)
    if not candidates:
        return False, "no node has a reworkable failed review with attempts remaining"
    return True, f"reworkable nodes: {candidates}"


@guard("replan_budget_available")
def _replan_budget_available(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    limit = min(task.max_plan_revisions, ctx.rt.policies.max_plan_revisions())
    if task.plan_revision_count >= limit:
        return False, f"plan_revision_count={task.plan_revision_count} has reached limit {limit}"
    return True, f"plan_revision_count={task.plan_revision_count} < limit {limit}"


@guard("synthesis_artifact_accepted")
def _synthesis_artifact_accepted(ctx: GuardContext) -> tuple[bool, str]:
    synth = ctx.nodes_of_kind(NodeKind.SYNTHESIS)
    if not synth:
        return False, "plan has no SYNTHESIS node"
    unfinished = [f"{n.ref}={n.status}" for n in synth if n.status not in NODE_DONE_STATES]
    if unfinished:
        return False, f"SYNTHESIS nodes not accepted: {unfinished}"
    return True, f"all {len(synth)} SYNTHESIS nodes accepted"


@guard("required_outputs_present")
def _required_outputs_present(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    accepted = {a.artifact_type for a in ctx.artifacts_for_task(task.id) if a.status == "ACCEPTED"}
    missing = sorted(t.value for t in task.required_outputs if t not in accepted)
    if missing:
        return False, f"accepted artifacts {sorted(t.value for t in accepted)}, missing {missing}"
    return True, f"all required outputs accepted: {sorted(t.value for t in task.required_outputs)}"


@guard("decision_is_decided")
def _decision_is_decided(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    decided = [d for d in ctx.decisions_for_task(task.id) if d.status == "DECIDED"]
    if not decided:
        return False, "task has no DECIDED decision"
    return True, f"decision {decided[-1].ref} is DECIDED"


@guard("decision_requires_no_action")
def _decision_requires_no_action(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    decided = [d for d in ctx.decisions_for_task(task.id) if d.status == "DECIDED"]
    if not decided:
        return False, "no DECIDED decision"
    latest = decided[-1]
    if not latest.requires_no_action:
        return False, f"decision_type={latest.decision_type} implies an action is needed"
    return True, f"decision_type={latest.decision_type} needs no action"


@guard("task_has_actions")
def _task_has_actions(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    actions = ctx.actions_for_task(task.id)
    if not actions:
        return False, "task has no Action"
    return True, f"{len(actions)} action(s): {[a.ref for a in actions]}"


@guard("approval_required_by_policy")
def _approval_required_by_policy(ctx: GuardContext) -> tuple[bool, str]:
    """Whether a human must approve before reality is touched.

    Approval is required only when policy says so. A sandbox dry run with no
    other trigger does not require it. The guard reports that outcome rather
    than forcing approval.
    """
    task = ctx.task()
    actions = ctx.actions_for_task(task.id)
    triggered: list[str] = []
    for action in actions:
        capability_class = (
            ctx.rt.capability(action.capability_key).risk_class
            if action.capability_key in ctx.rt.capability_index
            else None
        )
        outcome = ctx.rt.policies.approval_outcome(
            environment=action.target.environment,
            task_risk=task.risk_level,
            irreversible=not action.is_reversible,
            capability_risk_class=(
                capability_class if capability_class is not None else RiskClass.READ_ONLY
            ),
        )
        if outcome.required:
            triggered.append(f"{action.ref}: {outcome.basis}")
    if triggered:
        return True, "; ".join(triggered)
    return False, "no action requires human approval under current policy"


@guard("approval_granted")
def _approval_granted(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    actions = ctx.actions_for_task(task.id)
    if not actions:
        return False, "task has no Action waiting for a human grant"
    pending = [action.ref for action in actions if action.status != "APPROVED"]
    if pending:
        return False, f"actions not granted: {pending}"
    human_grants = [
        action.ref
        for action in actions
        if (check := action.check("APPROVAL")) is not None
        and check.verdict == "PASSED"
        and check.policy_ref == "POLICY-APPROVAL-V1#human_grant"
    ]
    if len(human_grants) != len(actions):
        return False, "APPROVED actions are missing a human GRANT check"
    return True, f"human grant recorded on {human_grants}"


@guard("approval_denied")
def _approval_denied(ctx: GuardContext) -> tuple[bool, str]:
    task = ctx.task()
    actions = ctx.actions_for_task(task.id)
    if not actions:
        return False, "task has no Action"
    rejected = [action.ref for action in actions if action.status == "REJECTED"]
    if len(rejected) != len(actions):
        return False, f"not all actions were denied: {[a.ref for a in actions]}"
    return True, f"human deny recorded on {rejected}"


@guard("all_actions_at_stop_state")
def _all_actions_at_stop_state(ctx: GuardContext) -> tuple[bool, str]:
    """Policy stop state, not a fixed terminal state.

    A dry run stops at APPROVED. Requiring SETTLED here would make every dry-run
    task permanently incompletable.
    """
    task = ctx.task()
    actions = ctx.actions_for_task(task.id)
    if not actions:
        return False, "task has no Action"
    machine = ctx.rt.machines.for_entity("Action")
    outstanding: list[str] = []
    for action in actions:
        stops = machine.stop_states.get(action.stop_state_key, ())
        if action.status not in stops:
            outstanding.append(f"{action.ref}={action.status} (stops: {list(stops)})")
    if outstanding:
        return False, f"actions not at stop state: {outstanding}"
    return True, ", ".join(f"{a.ref}={a.status} [{a.stop_state_key} stop state]" for a in actions)


# =====================================================================
# TaskPlan guards
# =====================================================================


@guard("plan_is_dag")
def _plan_is_dag(ctx: GuardContext) -> tuple[bool, str]:
    plan: TaskPlan = ctx.entity
    if not plan.is_dag:
        return False, "plan contains a cycle"
    order = plan.topological_order()
    return True, f"DAG with {len(order)} nodes in {len(plan.edges)} edges"


@guard("plan_nodes_reachable")
def _plan_nodes_reachable(ctx: GuardContext) -> tuple[bool, str]:
    """No orphan islands: every node must be reachable from an entry node."""
    plan: TaskPlan = ctx.entity
    entries = plan.entry_nodes()
    if not entries:
        return False, "plan has no entry node"
    seen: set[UUID] = set()
    queue: deque[UUID] = deque(entries)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(plan.downstream_of(node))
    unreachable = [str(n) for n in plan.node_ids if n not in seen]
    if unreachable:
        return False, f"unreachable nodes: {unreachable}"
    return True, f"all {len(plan.node_ids)} nodes reachable from {len(entries)} entry node(s)"


@guard("plan_no_blocking_review")
def _plan_no_blocking_review(ctx: GuardContext) -> tuple[bool, str]:
    return True, "no plan-level review configured in V0.1"


@guard("plan_is_only_active")
def _plan_is_only_active(ctx: GuardContext) -> tuple[bool, str]:
    plan: TaskPlan = ctx.entity
    others = [
        p.ref
        for p in ctx.rt.repos.plans.all()
        if p.task_id == plan.task_id and p.id != plan.id and p.status == "ACTIVE"
    ]
    if others:
        return False, f"task already has active plan(s) {others}"
    return True, "no competing active plan"


@guard("plan_exit_nodes_settled")
def _plan_exit_nodes_settled(ctx: GuardContext) -> tuple[bool, str]:
    plan: TaskPlan = ctx.entity
    nodes = {n.id: n for n in ctx.plan_nodes(plan)}
    pending = [
        nodes[nid].ref for nid in plan.exit_nodes() if nodes[nid].status not in NODE_DONE_STATES
    ]
    if pending:
        return False, f"exit nodes not settled: {pending}"
    return True, "all exit nodes settled"


# =====================================================================
# TaskNode guards
# =====================================================================


@guard("node_dependencies_satisfied")
def _node_dependencies_satisfied(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    plan = ctx.rt.repos.plans.get(node.plan_id)
    upstream = plan.upstream_of(node.id)
    if not upstream:
        return True, "entry node, no dependencies"
    blocking: list[str] = []
    for nid in upstream:
        dep = ctx.rt.repos.nodes.get(nid)
        if dep.status not in NODE_DONE_STATES:
            blocking.append(f"{dep.ref}={dep.status}")
    if blocking:
        return False, f"upstream not done: {blocking}"
    return True, f"all {len(upstream)} upstream node(s) done"


@guard("node_has_open_assignment")
def _node_has_open_assignment(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    open_states = ("PROPOSED", "OFFERED", "ACCEPTED", "IN_PROGRESS")
    matches = [
        a
        for a in ctx.assignments_for_node(node.id)
        if a.attempt == node.attempt and a.status in open_states
    ]
    if not matches:
        return False, f"no open assignment for {node.ref} attempt {node.attempt}"
    return True, f"assignment {matches[-1].ref} in {matches[-1].status}"


@guard("node_has_artifact")
def _node_has_artifact(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    artifacts = [
        a
        for a in ctx.rt.repos.artifacts.all()
        if a.node_id == node.id and a.status in ("SUBMITTED", "UNDER_REVIEW", "ACCEPTED")
    ]
    if not artifacts:
        return False, f"{node.ref} produced no submitted artifact"
    return True, f"artifact(s) {[a.ref for a in artifacts]}"


@guard("node_review_required")
def _node_review_required(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    if not node.review_policy.required:
        return False, "node review policy says review not required"
    reviews = [r for r in ctx.reviews_for_node(node.id) if r.round == node.attempt]
    if not reviews:
        return False, f"review required but none created for attempt {node.attempt}"
    return True, f"review(s) {[r.ref for r in reviews]} pending for attempt {node.attempt}"


@guard("node_review_not_required")
def _node_review_not_required(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    if node.review_policy.required:
        return False, "node review policy requires review"
    return True, "review not required by policy"


@guard("node_reviews_resolved")
def _node_reviews_resolved(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    reviews = [r for r in ctx.reviews_for_node(node.id) if r.round == node.attempt]
    if not reviews:
        return False, "no review for the current attempt"
    if len(reviews) < node.review_policy.min_reviewers:
        return False, f"{len(reviews)} review(s) < min_reviewers {node.review_policy.min_reviewers}"
    bad = [f"{r.ref}={r.status}" for r in reviews if r.status not in ("PASSED", "CONDITIONAL")]
    if bad:
        return False, f"reviews not passing: {bad}"
    return True, f"reviews passing: {[f'{r.ref}={r.status}' for r in reviews]}"


@guard("node_review_failed_reworkable")
def _node_review_failed_reworkable(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    reviews = [r for r in ctx.reviews_for_node(node.id) if r.round == node.attempt]
    reworkable = [r for r in reviews if r.is_reworkable]
    if not reworkable:
        failed = [f"{r.ref}={r.verdict}" for r in reviews if r.status == "FAILED"]
        if failed:
            return False, f"failed review has no required_changes, not reworkable: {failed}"
        return False, "no failed review for the current attempt"
    return True, (
        f"review {reworkable[-1].ref} FAILED with required_changes "
        f"{list(reworkable[-1].required_changes)}"
    )


@guard("node_attempts_available")
def _node_attempts_available(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    if not node.attempts_available:
        return False, f"attempt {node.attempt} of max {node.max_attempts}"
    return True, f"attempt {node.attempt} of max {node.max_attempts}, rework allowed"


@guard("node_attempts_exhausted")
def _node_attempts_exhausted(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    if node.attempts_available:
        return False, f"attempt {node.attempt} of {node.max_attempts}, retries remain"
    return True, f"attempts exhausted at {node.attempt}/{node.max_attempts}"


@guard("node_condition_false")
def _node_condition_false(ctx: GuardContext) -> tuple[bool, str]:
    node: TaskNode = ctx.entity
    if not node.skip_condition:
        return False, "node declares no skip_condition"
    return bool(ctx.extra.get("condition_false")), f"skip_condition='{node.skip_condition}'"


# =====================================================================
# TaskAssignment guards
# =====================================================================


@guard("assignment_permissions_within_role")
def _assignment_permissions_within_role(ctx: GuardContext) -> tuple[bool, str]:
    """Layer 3 can only narrow. An assignment cannot grant what a role never had."""
    assignment: TaskAssignment = ctx.entity
    agent = ctx.rt.repos.agents.get(assignment.assignee_agent_id)
    held = agent.effective_permissions(ctx.rt.role_map())
    excess = sorted(set(assignment.granted_permissions) - set(held))
    if excess:
        return False, f"assignment grants permissions the agent's roles do not hold: {excess}"
    return True, f"granted {sorted(assignment.granted_permissions)} ⊆ role permissions"


@guard("assignment_assignee_not_forbidden")
def _assignment_assignee_not_forbidden(ctx: GuardContext) -> tuple[bool, str]:
    assignment: TaskAssignment = ctx.entity
    node = ctx.rt.repos.nodes.get(assignment.node_id)
    if assignment.assignee_agent_id in node.forbidden_agent_ids:
        agent = ctx.rt.repos.agents.get(assignment.assignee_agent_id)
        return False, f"{agent.name} is on {node.ref}.forbidden_agent_ids"
    return True, "assignee not forbidden on this node"


@guard("assignment_has_result")
def _assignment_has_result(ctx: GuardContext) -> tuple[bool, str]:
    assignment: TaskAssignment = ctx.entity
    if not assignment.result_artifact_ids:
        return False, "assignment produced no artifact"
    return True, f"{len(assignment.result_artifact_ids)} artifact(s) produced"


# =====================================================================
# Agent guards
# =====================================================================


@guard("agent_has_active_role")
def _agent_has_active_role(ctx: GuardContext) -> tuple[bool, str]:
    agent: Agent = ctx.entity
    if not agent.role_ids:
        return False, "agent holds no role, so it holds no permissions"
    names = [ctx.rt.repos.roles.get(r).name for r in agent.role_ids]
    return True, f"roles: {names}"


@guard("agent_has_capacity")
def _agent_has_capacity(ctx: GuardContext) -> tuple[bool, str]:
    agent: Agent = ctx.entity
    if not agent.has_capacity:
        return (
            False,
            f"{agent.current_assignment_count}/{agent.max_concurrent_assignments} assignments",
        )
    return True, f"{agent.current_assignment_count}/{agent.max_concurrent_assignments} assignments"


@guard("agent_no_inflight_assignments")
def _agent_no_inflight_assignments(ctx: GuardContext) -> tuple[bool, str]:
    agent: Agent = ctx.entity
    inflight = [
        a.ref
        for a in ctx.rt.repos.assignments.all()
        if a.assignee_agent_id == agent.id and a.status in ("OFFERED", "ACCEPTED", "IN_PROGRESS")
    ]
    if inflight:
        return False, f"in-flight assignments block graceful exit: {inflight}"
    return True, "no in-flight assignments"


# =====================================================================
# Review guards
# =====================================================================


@guard("reviewer_is_independent")
def _reviewer_is_independent(ctx: GuardContext) -> tuple[bool, str]:
    """Four-Eyes, enforced as a transition precondition.

    The domain model already refuses to construct such a Review, so this is the
    second of two barriers. Both are cheap; a self-approved artifact is not.
    """
    review: Review = ctx.entity
    if review.reviewer_agent_id == review.producer_agent_id:
        return False, "reviewer is the producer (Four-Eyes violation)"
    reviewer = ctx.rt.repos.agents.get(review.reviewer_agent_id)
    producer = ctx.rt.repos.agents.get(review.producer_agent_id)
    return True, f"reviewer={reviewer.name} != producer={producer.name}"


@guard("reviewer_is_authorized")
def _reviewer_is_authorized(ctx: GuardContext) -> tuple[bool, str]:
    review: Review = ctx.entity
    reviewer = ctx.rt.repos.agents.get(review.reviewer_agent_id)
    held = reviewer.effective_permissions(ctx.rt.role_map())
    if "review.conduct" not in held:
        return False, f"{reviewer.name} lacks 'review.conduct'"
    if not reviewer.holds("governance.review"):
        return False, f"{reviewer.name} does not hold capability 'governance.review'"
    return True, f"{reviewer.name} holds review.conduct and governance.review"


@guard("review_no_blocking_findings")
def _review_no_blocking_findings(ctx: GuardContext) -> tuple[bool, str]:
    review: Review = ctx.entity
    blockers = review.blocking_findings
    if blockers:
        return False, f"{len(blockers)} BLOCKER finding(s): {[f.statement[:50] for f in blockers]}"
    majors = review.unresolved_major
    if majors:
        return False, f"{len(majors)} unresolved MAJOR finding(s)"
    return True, "no BLOCKER or unresolved MAJOR findings"


@guard("review_has_blocking_findings")
def _review_has_blocking_findings(ctx: GuardContext) -> tuple[bool, str]:
    review: Review = ctx.entity
    blockers = review.blocking_findings
    if not blockers:
        return False, "cannot fail a review with no BLOCKER finding"
    return True, f"{len(blockers)} BLOCKER finding(s)"


@guard("review_has_conditions")
def _review_has_conditions(ctx: GuardContext) -> tuple[bool, str]:
    review: Review = ctx.entity
    if not review.conditions:
        return False, "CONDITIONAL verdict requires at least one condition"
    return True, f"{len(review.conditions)} condition(s)"


# =====================================================================
# Artifact guards
# =====================================================================


@guard("artifact_findings_have_evidence")
def _artifact_findings_have_evidence(ctx: GuardContext) -> tuple[bool, str]:
    artifact: Artifact = ctx.entity
    if not artifact.findings:
        return False, "artifact has no findings"
    if not artifact.evidence:
        return False, "artifact has no evidence"
    known = {e.id for e in artifact.evidence}
    for finding in artifact.findings:
        missing = [str(e) for e in finding.evidence_ids if e not in known]
        if missing:
            return False, f"finding cites missing evidence {missing}"
    return True, (
        f"{len(artifact.findings)} finding(s) traceable to {len(artifact.evidence)} evidence item(s)"
    )


# =====================================================================
# Decision guards
# =====================================================================


@guard("decision_has_evidence")
def _decision_has_evidence(ctx: GuardContext) -> tuple[bool, str]:
    decision: Decision = ctx.entity
    if not decision.based_on_artifact_ids:
        return False, "decision cites no artifacts"
    return True, f"based on {len(decision.based_on_artifact_ids)} artifact(s)"


@guard("decision_has_alternatives")
def _decision_has_alternatives(ctx: GuardContext) -> tuple[bool, str]:
    """A decision with no rejected alternative was not a decision.

    This is what makes decision quality judgeable separately from outcome quality.
    """
    decision: Decision = ctx.entity
    if not decision.alternatives_considered:
        return False, "no alternatives recorded; decision quality would be unjudgeable"
    return True, f"{len(decision.alternatives_considered)} alternative(s) considered"


@guard("decision_has_measurement_plan")
def _decision_has_measurement_plan(ctx: GuardContext) -> tuple[bool, str]:
    decision: Decision = ctx.entity
    if decision.measurement_plan is None:
        return False, "no measurement plan; we could not tell later whether this was right"
    return True, f"metrics {list(decision.measurement_plan.metrics)}"


@guard("decision_requires_approval")
def _decision_requires_approval(ctx: GuardContext) -> tuple[bool, str]:
    decision: Decision = ctx.entity
    if not decision.requires_approval:
        return False, "decision does not require approval"
    return True, f"approval required by {decision.approval_policy_ref}"


@guard("decision_approval_not_required")
def _decision_approval_not_required(ctx: GuardContext) -> tuple[bool, str]:
    decision: Decision = ctx.entity
    if decision.requires_approval:
        return False, f"approval required by {decision.approval_policy_ref}"
    return True, "no approval required under current policy"


@guard("decision_maker_has_authority")
def _decision_maker_has_authority(ctx: GuardContext) -> tuple[bool, str]:
    decision: Decision = ctx.entity
    if decision.decided_by_agent_id is None:
        return False, "decision has no decided_by_agent_id"
    agent = ctx.rt.repos.agents.get(decision.decided_by_agent_id)
    roles = ctx.rt.role_map()
    if "decision.make" not in agent.effective_permissions(roles):
        return False, f"{agent.name} lacks 'decision.make'"
    ceiling = max(
        (roles[r].max_risk_level for r in agent.role_ids if r in roles),
        default=RiskLevel.ROUTINE,
    )
    if decision.risk_level > ceiling:
        return False, (
            f"{agent.name} role ceiling {ceiling} below decision risk {decision.risk_level}"
        )
    return True, f"{agent.name} authorised up to {ceiling}, decision at {decision.risk_level}"


@guard("decision_approval_granted")
def _decision_approval_granted(ctx: GuardContext) -> tuple[bool, str]:
    return False, "Approval object is not implemented in V0.1 (declared scope exclusion)"


# =====================================================================
# Action guards
# =====================================================================


@guard("action_has_decision")
def _action_has_decision(ctx: GuardContext) -> tuple[bool, str]:
    """No action without a decision that authorized it (invariant I10)."""
    action: Action = ctx.entity
    decision = ctx.rt.repos.decisions.find(action.decision_id)
    if decision is None:
        return False, f"decision {action.decision_id} not found"
    if decision.status != "DECIDED":
        return False, f"decision {decision.ref} is {decision.status}, not DECIDED"
    return True, f"authorised by decision {decision.ref}"


@guard("action_payload_valid")
def _action_payload_valid(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    if not action.target.system.strip() or not action.target.operation.strip():
        return False, "action target must name a system and an operation"
    allowed = ctx.rt.policies.allowed_environments(ctx.task().risk_level)
    if action.target.environment.value not in allowed:
        return False, (
            f"environment {action.target.environment.value} not permitted at task risk "
            f"{ctx.task().risk_level} (allowed: {list(allowed)})"
        )
    return True, (
        f"target {action.target.system}/{action.target.operation} in "
        f"{action.target.environment.value}"
    )


@guard("action_idempotency_key_unique")
def _action_idempotency_key_unique(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    clashes = [
        a.ref
        for a in ctx.rt.repos.actions.all()
        if a.id != action.id and a.idempotency_key == action.idempotency_key
    ]
    if clashes:
        return False, f"idempotency_key already used by {clashes}"
    return True, f"idempotency_key '{action.idempotency_key}' is unique"


@guard("action_permission_granted")
def _action_permission_granted(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    check = action.check("PERMISSION")
    if check is None:
        return False, "PERMISSION check has not been recorded"
    if check.verdict == "FAILED":
        return False, f"PERMISSION failed: {check.detail}"
    return True, f"PERMISSION {check.verdict} ({check.policy_ref})"


@guard("action_risk_cleared")
def _action_risk_cleared(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    check = action.check("RISK")
    if check is None:
        return False, "RISK check has not been recorded"
    if check.verdict == "FAILED":
        return False, f"RISK failed: {check.detail}"
    return True, f"RISK {check.verdict} ({check.policy_ref})"


@guard("action_approval_satisfied")
def _action_approval_satisfied(ctx: GuardContext) -> tuple[bool, str]:
    """PASSED or WAIVED both satisfy; missing does not.

    Recording a WAIVED verdict with its policy reference keeps the audit chain
    complete, which skipping the check would not.
    """
    action: Action = ctx.entity
    check = action.check("APPROVAL")
    if check is None:
        return False, "APPROVAL check has not been recorded"
    if check.verdict == "FAILED":
        return False, f"APPROVAL failed: {check.detail}"
    return True, f"APPROVAL {check.verdict} ({check.policy_ref}): {check.detail}"


@guard("action_is_not_dry_run")
def _action_is_not_dry_run(ctx: GuardContext) -> tuple[bool, str]:
    """The mechanical guarantee that a SANDBOX dry run never touches reality.

    This is the one guard whose failure is a success: in V0.1 every action is a
    dry run, so this guard is what makes ``execute`` unreachable.
    """
    action: Action = ctx.entity
    if action.dry_run:
        return False, (
            f"{action.ref} is dry_run=true in {action.target.environment.value}; "
            f"policy stop state is {ctx.rt.policies.dry_run_stop_state()}"
        )
    return True, (
        f"{action.ref} is dry_run=false in {action.target.environment.value}; "
        "execute is reachable subject to remaining guards"
    )


@guard("action_environment_is_sandbox")
def _action_environment_is_sandbox(ctx: GuardContext) -> tuple[bool, str]:
    """Sandbox execute must not be a back door into LIVE or PAPER."""
    action: Action = ctx.entity
    if action.target.environment is not Environment.SANDBOX:
        return False, (
            f"{action.ref} targets {action.target.environment.value}; "
            "sandbox execute cannot leave SANDBOX"
        )
    return True, f"{action.ref} targets SANDBOX"


@guard("action_all_checks_passed")
def _action_all_checks_passed(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    if not action.all_checks_passed:
        recorded = {c.kind: c.verdict for c in action.checks}
        return False, f"checks incomplete or failed: {recorded}"
    return True, f"all checks passed: {[f'{c.kind}={c.verdict}' for c in action.checks]}"


@guard("organization_not_halted")
def _organization_not_halted(ctx: GuardContext) -> tuple[bool, str]:
    org = ctx.rt.organization()
    if org.is_halted:
        return False, "organization operating_mode is HALTED (kill switch engaged)"
    return True, f"operating_mode={org.operating_mode}"


@guard("action_verification_matched")
def _action_verification_matched(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    if action.verification is None:
        return False, "no verification recorded; EXECUTED is not the same as it worked"
    if not action.verification.matched:
        return False, (
            f"verification mismatch: expected {action.verification.expected}, "
            f"observed {action.verification.observed}"
        )
    return True, "post-execution read-back matched expectation"


@guard("action_retry_available")
def _action_retry_available(ctx: GuardContext) -> tuple[bool, str]:
    action: Action = ctx.entity
    if action.execution_attempts >= action.max_execution_attempts:
        return False, f"{action.execution_attempts}/{action.max_execution_attempts} attempts used"
    return True, f"{action.execution_attempts}/{action.max_execution_attempts} attempts used"


# Keep the import used so linters do not strip it; ArtifactType/Environment are
# referenced by type annotations in helpers above.
_ = (ArtifactType, Environment)
