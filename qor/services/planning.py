"""Planning -- turn an objective into a capability-addressed DAG.

The planner is untrusted. Whatever it returns is validated here before anything is
persisted: capabilities must exist in the catalog, the graph must be acyclic and
fully reachable, node count must be within policy, and the plan must cover every
artifact type the task requires.

An important distinction is made between two failure modes that look similar:

* a capability that **is not in the catalog** -> invalid plan, rejected here.
* a capability that **no agent currently holds** (a single unstaffed key) -> a
  legal plan, and delegation blocks that node with CAPABILITY_GAP (T11).
* a **combination** of catalog-staffed capabilities that no one agent holds
  together -> organizationally unassignable. That is a planner feasibility
  failure: feed a structured CapabilityGap back for one bounded repair. Do not
  silently split nodes or drop a capability at matching time.

"""

from __future__ import annotations

from uuid import UUID

from qor.domain.decision import Decision
from qor.domain.enums import ArtifactType
from qor.domain.outcome import FOLLOW_THROUGH_FULFILLED, Outcome
from qor.domain.task import ReviewPolicy, Task, TaskEdge, TaskNode, TaskPlan
from qor.kernel.actor import ActorRef
from qor.kernel.errors import InvariantViolation, QORError
from qor.ports.llm import LLMUnavailable
from qor.ports.planner import (
    BINDING_ADDRESS_CONTRADICTED,
    BINDING_ADDRESS_FULFILLED_RESPONSE,
    BindingConstraint,
    CapabilityOffer,
    LessonFact,
    OutcomeFact,
    PlanDraft,
    PlannerPort,
    PlanRequest,
    binding_constraint_violations,
)
from qor.services.authorization import Authorizer
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["PlanningService", "PlanRejected"]


class PlanRejected(QORError):
    """The planner produced something the organization will not execute."""


class PlanningService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        planner: PlannerPort,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._planner = planner

    # ---- public --------------------------------------------------------

    def plan(self, task: Task, *, feedback: tuple[str, ...] = ()) -> tuple[Task, TaskPlan]:
        """Move the task OPEN -> PLANNING -> READY, producing an ACTIVE plan."""
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()

        if task.status == "OPEN":
            task = self._rt.repos.tasks.save(
                self._engine.fire(
                    task,
                    "start_planning",
                    actor=actor,
                    reason_code="ROUTINE_PROGRESSION",
                    reason=f"{owner.name} took ownership and began planning",
                    correlation_id=task.id,
                ).entity
            )

        # The planner runs under an audited permission: planning is an exercise of
        # authority, not a free action.
        self._authz.check(
            actor=actor,
            permission="plan.create",
            target_type="Task",
            target_id=task.id,
            target_ref=task.ref,
            correlation_id=task.id,
        )

        request = self.build_request(task, feedback)
        try:
            draft = self._planner.plan(request)
        except LLMUnavailable:
            raise
        except QORError as exc:
            # Adapter parse failures (e.g. PlannerOutputInvalid) are organizational
            # rejections, not a reason to swap in a stub.
            raise PlanRejected(str(exc)) from exc
        try:
            self._validate(task, draft, request)
            self._reject_unassignable_combinations(draft)
        except PlanRejected as rejected:
            # One T19-style repair: feed the structured rejection back. The first
            # illegal draft has already failed; salvage by dropping constraints is
            # forbidden. Combination CapabilityGap uses this same bound.
            request = self.build_request(task, (*feedback, str(rejected)))
            try:
                draft = self._planner.plan(request)
            except LLMUnavailable:
                raise
            except QORError as exc:
                raise PlanRejected(str(exc)) from exc
            self._validate(task, draft, request)
            self._reject_unassignable_combinations(draft)

        plan, nodes = self._materialise(task, draft, actor)
        plan = self._activate(plan, actor, task)

        task = self._rt.repos.tasks.get(task.id)
        task.active_plan_id = plan.id
        task = self._rt.repos.tasks.save(task)

        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "plan_approved",
                actor=actor,
                reason_code="PLAN_APPROVED",
                reason=(
                    f"plan {plan.ref} activated with {len(nodes)} nodes "
                    f"({self._planner.kind} planner): {draft.strategy_rationale[:120]}"
                ),
                correlation_id=task.id,
            ).entity
        )
        return task, self._rt.repos.plans.get(plan.id)

    def replan(self, task: Task, *, feedback: tuple[str, ...]) -> tuple[Task, TaskPlan]:
        """Supersede the active plan with revision n+1.

        The old plan is not edited. It is superseded, so "what did we originally
        intend" remains answerable after the fact.
        """
        owner = self._rt.repos.agents.get(task.owner_agent_id)  # type: ignore[arg-type]
        actor = owner.as_actor()

        task = self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "replan",
                actor=actor,
                reason_code="PLAN_SUPERSEDED",
                reason="; ".join(feedback) or "replanning requested",
                correlation_id=task.id,
            ).entity
        )
        if task.active_plan_id is not None:
            old = self._rt.repos.plans.get(task.active_plan_id)
            self._rt.repos.plans.save(
                self._engine.fire(
                    old,
                    "supersede",
                    actor=actor,
                    reason_code="PLAN_SUPERSEDED",
                    reason="replaced by a later revision",
                    correlation_id=task.id,
                ).entity
            )
        task = self._rt.repos.tasks.get(task.id)
        task.plan_revision_count += 1
        task = self._rt.repos.tasks.save(task)
        return self.plan(task, feedback=feedback)

    # ---- internals -----------------------------------------------------

    def build_request(self, task: Task, feedback: tuple[str, ...] = ()) -> PlanRequest:
        """Assemble what the planner is allowed to see.

        Note what goes in: capabilities. Note what does not: agents. The planner
        cannot name an executor because it has never been shown one.
        """
        offers = tuple(
            CapabilityOffer(
                key=cap.key,
                name=cap.name,
                description=cap.description,
                risk_class=cap.risk_class.name,
            )
            for cap in self._rt.repos.capabilities.all()
            # Catalog order is the request order. Do not sort by capability key.
            # Capabilities that declare extra required_permissions are organizational
            # machinery (review / planning / decision / gated action). Offering them
            # lets a planner schedule a node that nobody can execute under
            # artifact.create — Live T28 blocked exactly that way.
            if cap.risk_class.name != "REALITY_AFFECTING" and not cap.required_permissions
        )
        facts, constraints = self._organization_facts(task)
        return PlanRequest(
            task_ref=task.ref,
            title=task.title,
            objective=task.objective,
            risk_level=task.risk_level,
            required_outputs=task.required_outputs,
            available_capabilities=offers,
            max_nodes=self._rt.policies.max_nodes(),
            previous_failure_feedback=feedback,
            prior_outcomes=facts,
            prior_lessons=self._prior_lessons(task),
            binding_constraints=constraints,
        )

    def _organization_facts(
        self, task: Task
    ) -> tuple[tuple[OutcomeFact, ...], tuple[BindingConstraint, ...]]:
        """Inject relevant AUTHORITATIVE Outcomes. Binding is a subset of visibility.

        Relevance is a substring match of ``subject_key`` in ``task.objective``.
        No embedding, no LLM judge, no fuzzy match.
        """
        facts: list[OutcomeFact] = []
        constraints: list[BindingConstraint] = []
        decisions = {d.id: d for d in self._rt.repos.decisions.all()}
        for outcome in sorted(self._rt.repos.outcomes.all(), key=lambda o: o.ref):
            if outcome.status != "AUTHORITATIVE":
                continue
            if outcome.subject_key not in task.objective:
                continue
            decision = decisions.get(outcome.decision_id)
            fulfilling = _fulfilling_decision(self._rt, outcome)
            facts.append(
                OutcomeFact(
                    ref=outcome.ref,
                    decision_ref=decision.ref if decision is not None else "",
                    subject_key=outcome.subject_key,
                    verdict=outcome.verdict,
                    contradicted_decision_type=outcome.contradicted_decision_type,
                    confidence=outcome.confidence,
                    status=outcome.status,
                    statement=outcome.statement,
                    follow_through_status=outcome.follow_through_status,
                    fulfilling_decision_ref=fulfilling.ref if fulfilling is not None else "",
                )
            )
            if outcome.verdict == "CONTRADICTED" and outcome.confidence == "HIGH":
                constraints.append(
                    BindingConstraint(
                        kind=BINDING_ADDRESS_CONTRADICTED,
                        outcome_ref=outcome.ref,
                        subject_key=outcome.subject_key,
                        contradicted_decision_type=outcome.contradicted_decision_type,
                    )
                )
            if fulfilling is not None:
                constraints.append(
                    BindingConstraint(
                        kind=BINDING_ADDRESS_FULFILLED_RESPONSE,
                        outcome_ref=outcome.ref,
                        subject_key=outcome.subject_key,
                        contradicted_decision_type=outcome.contradicted_decision_type,
                        decision_ref=fulfilling.ref,
                    )
                )
        return tuple(facts), tuple(constraints)

    def _prior_lessons(self, task: Task) -> tuple[LessonFact, ...]:
        """Inject APPROVED Lessons as context. Never as BindingConstraint.

        Relevance is the same exact ``subject_key in objective`` rule as Outcomes.
        A Lesson whose source Outcomes are all no longer AUTHORITATIVE is not injected.
        """
        facts: list[LessonFact] = []
        outcomes = {item.id: item for item in self._rt.repos.outcomes.all()}
        for lesson in sorted(self._rt.repos.lessons.all(), key=lambda item: item.ref):
            if lesson.status != "APPROVED":
                continue
            if lesson.applicability_subject_key not in task.objective:
                continue
            live = [
                outcomes[oid]
                for oid in lesson.source_outcome_ids
                if oid in outcomes and outcomes[oid].status == "AUTHORITATIVE"
            ]
            if not live:
                continue
            facts.append(
                LessonFact(
                    ref=lesson.ref,
                    applicability_subject_key=lesson.applicability_subject_key,
                    statement=lesson.statement,
                    source_outcome_refs=tuple(item.ref for item in live),
                )
            )
        return tuple(facts)

    def _validate(self, task: Task, draft: PlanDraft, request: PlanRequest) -> None:
        catalog = set(self._rt.capability_index)
        unknown: list[str] = []
        for node in draft.nodes:
            unknown += [c for c in node.required_capabilities if c not in catalog]
        if unknown:
            raise PlanRejected(
                f"plan requires capabilities that do not exist in the catalog: {sorted(set(unknown))}"
            )

        limit = self._rt.policies.max_nodes()
        if len(draft.nodes) > limit:
            raise PlanRejected(f"plan has {len(draft.nodes)} nodes, policy limit is {limit}")

        if self._rt.policies.enforce_output_coverage():
            produced = {n.expected_artifact_type for n in draft.nodes}
            missing = sorted(t.value for t in task.required_outputs if t not in produced)
            if missing:
                raise PlanRejected(
                    f"plan does not produce required outputs {missing}; "
                    f"it produces {sorted(t.value for t in produced)}"
                )

        # A synthesis node that consumes nothing is not synthesis.
        for node in draft.nodes:
            if node.node_kind == "SYNTHESIS" and not node.depends_on:
                raise PlanRejected(
                    f"synthesis node '{node.key}' has no upstream dependencies; "
                    "it would have nothing to integrate"
                )

        violations = binding_constraint_violations(draft.nodes, request.binding_constraints)
        if violations:
            raise PlanRejected("; ".join(violations))

    def _reject_unassignable_combinations(self, draft: PlanDraft) -> None:
        """Reject WORK nodes whose capability *set* no agent holds together.

        Single unstaffed capabilities stay legal here so T11 can still surface
        CAPABILITY_GAP at assignment. Combinations of otherwise staffed
        capabilities are a planner error: the organization can do each piece,
        just not as one node.
        """
        agents = tuple(self._rt.repos.agents.all())
        messages: list[str] = []
        for node in draft.nodes:
            if node.node_kind != "WORK":
                continue
            caps = tuple(node.required_capabilities)
            if len(caps) < 2:
                continue
            if any(not any(agent.holds(cap) for agent in agents) for cap in caps):
                continue
            if any(all(agent.holds(cap) for cap in caps) for agent in agents):
                continue
            messages.append(
                "CapabilityGap: WORK node "
                f"'{node.key}' requires {list(caps)} together, but no agent holds "
                "all of those capabilities at once. Each capability is staffed "
                "individually. Split into separate nodes or choose a jointly held "
                "set. Do not name agents."
            )
        if messages:
            raise PlanRejected("; ".join(messages))

    def _materialise(
        self, task: Task, draft: PlanDraft, actor: ActorRef
    ) -> tuple[TaskPlan, tuple[TaskNode, ...]]:
        now = self._rt.clock.now()
        pid, pref = self._rt.mint("PLAN")

        key_to_id: dict[str, UUID] = {}
        nodes: list[TaskNode] = []
        for spec in draft.nodes:
            nid, nref = self._rt.mint("NODE")
            key_to_id[spec.key] = nid
            nodes.append(
                TaskNode(
                    id=nid,
                    ref=nref,
                    status="PENDING",
                    created_at=now,
                    created_by=actor,
                    updated_at=now,
                    task_id=task.id,
                    plan_id=pid,
                    name=spec.name,
                    objective=spec.objective,
                    node_kind=spec.node_kind,
                    required_capabilities=spec.required_capabilities,
                    expected_artifact_type=spec.expected_artifact_type,
                    review_policy=self._review_policy_for(
                        task, spec.required_capabilities, spec.review_required
                    ),
                    max_attempts=self._rt.policies.max_attempts_per_node(),
                    constraints=task.constraints,
                    addresses_outcome_ref=spec.addresses_outcome_ref,
                    addresses_decision_ref=spec.addresses_decision_ref,
                )
            )

        edges = tuple(
            TaskEdge(
                from_node_id=key_to_id[dep],
                to_node_id=key_to_id[spec.key],
                reason=f"{spec.key} consumes output of {dep}",
            )
            for spec in draft.nodes
            for dep in spec.depends_on
        )

        plan = TaskPlan(
            id=pid,
            ref=pref,
            status="DRAFT",
            created_at=now,
            created_by=actor,
            updated_at=now,
            task_id=task.id,
            revision=task.plan_revision_count + 1,
            node_ids=tuple(n.id for n in nodes),
            edges=edges,
            strategy_rationale=draft.strategy_rationale,
            planner_kind=self._planner.kind,
            planner_provider=getattr(self._planner, "provider", self._planner.kind),
            expected_outputs=tuple(draft.expected_outputs)
            or tuple({n.expected_artifact_type for n in nodes}),
            supersedes_plan_id=task.active_plan_id,
        )
        plan = self._rt.repos.plans.insert(plan)

        self._engine.record_creation(
            plan,
            actor=actor,
            reason_code="ROUTINE_PROGRESSION",
            reason=f"{self._planner.kind} planner proposed: {draft.strategy_rationale}",
            correlation_id=task.id,
            event_name="qor.plan.created",
            payload={
                "planner": self._planner.kind,
                "provider": getattr(self._planner, "provider", self._planner.kind),
                "node_count": len(nodes),
                "rationale": draft.strategy_rationale,
            },
        )

        # Insert nodes after the plan so their creation records can cite it.
        for node in nodes:
            stored = self._rt.repos.nodes.insert(node)
            self._engine.record_creation(
                stored,
                actor=actor,
                reason_code="ROUTINE_PROGRESSION",
                reason=(
                    f"node '{stored.name}' requires {list(stored.required_capabilities)}; "
                    f"review {'required' if stored.review_policy.required else 'not required'}"
                ),
                correlation_id=task.id,
                event_name="qor.node.created",
                payload={
                    "name": stored.name,
                    "node_kind": stored.node_kind,
                    "required_capabilities": list(stored.required_capabilities),
                    "expected_artifact_type": stored.expected_artifact_type.value,
                },
            )
        return self._rt.repos.plans.get(plan.id), tuple(nodes)

    def _activate(self, plan: TaskPlan, actor: ActorRef, task: Task) -> TaskPlan:
        for transition, code, reason in (
            ("propose", "ROUTINE_PROGRESSION", f"plan {plan.ref} proposed for approval"),
            ("approve", "PLAN_APPROVED", f"plan {plan.ref} approved by {actor}"),
            ("activate", "PLAN_APPROVED", f"plan {plan.ref} became the task's active plan"),
        ):
            plan = self._rt.repos.plans.save(
                self._engine.fire(
                    plan,
                    transition,
                    actor=actor,
                    reason_code=code,
                    reason=reason,
                    correlation_id=task.id,
                ).entity
            )
        return plan

    def _review_policy_for(
        self, task: Task, capabilities: tuple[str, ...], planner_wanted_review: bool
    ) -> ReviewPolicy:
        """Policy overrides the planner.

        A planner that decides its own work does not need checking is exactly the
        situation review exists for, so ``planner_wanted_review`` can only add
        review, never remove it.
        """
        mandatory: list[str] = []
        for key in capabilities:
            cap = self._rt.capability(key)
            mandatory += list(cap.mandatory_review_types)

        requirement = self._rt.policies.review_requirement(
            task_risk=task.risk_level,
            capabilities=capabilities,
            capability_review_types=tuple(dict.fromkeys(mandatory)),
        )
        return ReviewPolicy(
            required=requirement.required or planner_wanted_review,
            review_types=requirement.review_types,
            min_reviewers=requirement.min_reviewers,
            reviewer_capability=requirement.reviewer_capability,
        )


def _fulfilling_decision(runtime: Runtime, outcome: Outcome) -> Decision | None:
    if outcome.status != "AUTHORITATIVE":
        return None
    if outcome.follow_through_status != FOLLOW_THROUGH_FULFILLED:
        return None
    if outcome.follow_through_task_id is None:
        return None
    task = runtime.repos.tasks.find(outcome.follow_through_task_id)
    if task is None or task.source_outcome_id != outcome.id:
        return None
    matches = [
        decision
        for decision in runtime.repos.decisions.all()
        if decision.task_id == task.id
        and decision.status == "DECIDED"
        and decision.addresses_outcome_ref == outcome.ref
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.ref)[-1]


_ = (ArtifactType, InvariantViolation)
