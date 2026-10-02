"""Decision -- commit the organization, without touching anything.

The AI CIO proposes; the guards decide whether the proposal is admissible. The
three that matter are evidence, alternatives and a measurement plan. A decision
that fails any of them is not a cautious decision, it is an unaccountable one.

Whether a human must approve is computed from policy here rather than asserted by
the proposer, so an agent cannot mark its own decision as not needing approval.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

from qor.domain.action import Action
from qor.domain.artifact import Artifact
from qor.domain.decision import (
    LEDGER_OBSERVABLE_METRICS,
    Alternative,
    Decision,
    MeasurementEvaluation,
    MeasurementPlan,
    SuccessCheck,
)
from qor.domain.enums import DecisionType, Environment, RiskClass, RiskLevel
from qor.domain.task import Task
from qor.kernel.actor import ActorRef
from qor.kernel.errors import QORError
from qor.ports.agent_runtime import AgentRuntimePort, DecisionProposal, DecisionRequest
from qor.ports.world import WorldReader
from qor.services.authorization import Authorizer
from qor.services.outcomes import OutcomeService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine


def _strings(value: object) -> tuple[str, ...]:
    """Read a loosely typed proposal field as a tuple of strings.

    The agent runtime port deliberately types proposal payloads as plain data, so
    coercion happens here at the boundary rather than trusting the shape.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


__all__ = ["DecisionService"]


class DecisionService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        agents: AgentRuntimePort,
        outcomes: OutcomeService,
        world_reader: WorldReader | None = None,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._agents = agents
        self._outcomes = outcomes
        self._world_reader = world_reader

    def decide(self, task: Task) -> tuple[Task, Decision, DecisionProposal]:
        if task.owner_agent_id is None:
            raise QORError(f"{task.ref} has no owner, so nobody can decide")
        owner = self._rt.repos.agents.get(task.owner_agent_id)
        actor = owner.as_actor()

        accepted = tuple(
            a
            for a in sorted(self._rt.repos.artifacts.all(), key=lambda x: x.ref)
            if a.task_id == task.id and a.status == "ACCEPTED"
        )
        proposal = self._agents.propose_decision(
            owner.id,
            DecisionRequest(
                task_ref=task.ref,
                task_objective=task.objective,
                accepted_artifacts=accepted,
                risk_level=task.risk_level.name,
                permitted_environments=self._rt.policies.allowed_environments(task.risk_level),
                addresses_outcome_ref=self._source_outcome_ref(task),
            ),
        )

        decision = self._materialise(task, proposal, accepted, actor)
        decision = self._rt.repos.decisions.save(
            self._engine.fire(
                decision,
                "propose",
                actor=actor,
                reason_code="DECISION_MADE",
                reason=(
                    f"{owner.name} proposed: {proposal.statement} "
                    f"(based on {len(accepted)} accepted artifact(s))"
                ),
                correlation_id=task.id,
            ).entity
        )
        decision = self._rt.repos.decisions.save(
            self._engine.fire(
                decision,
                "decide",
                actor=actor,
                reason_code="DECISION_MADE",
                reason=proposal.rationale,
                correlation_id=task.id,
                payload={
                    "decision_type": decision.decision_type.value,
                    "statement": decision.statement,
                    "alternatives": [a.option for a in decision.alternatives_considered],
                    "addresses_outcome_ref": decision.addresses_outcome_ref,
                },
            ).entity
        )
        self._outcomes._note_decision_addressed(decision)  # noqa: SLF001 — governance, not an agent API
        return task, decision, proposal

    def evaluate_measurement(
        self,
        decision_id: UUID,
        requested_by_agent_id: UUID,
        *,
        human_requested: bool = False,
    ) -> Decision:
        decision = self._rt.repos.decisions.get(decision_id)
        requester = self._rt.repos.agents.get(requested_by_agent_id)
        actor = requester.as_actor()
        self._authz.check(
            actor=actor,
            permission="decision.evaluate_measurement",
            target_type="Decision",
            target_id=decision.id,
            target_ref=decision.ref,
        )
        plan = decision.measurement_plan
        if plan is None:
            raise QORError(f"{decision.ref} has no measurement_plan to evaluate")
        actions = self._actions_for(decision.id)
        eligibility = self._eligibility(decision, plan, actions, human_requested)
        observed, sources, action = self._collect_observation(plan, actions)
        verdict = self._verdict(plan.success_checks, observed)
        evaluation = MeasurementEvaluation(
            verdict=verdict,
            observed=observed,
            criteria_applied=plan.success_checks,
            observation_source=",".join(sources) if sources else "",
            eligibility=eligibility,
            evaluated_at=self._rt.clock.now(),
            action_ref=action.ref if action is not None else "",
        )
        decision.measurement_evaluations = (*decision.measurement_evaluations, evaluation)
        decision.updated_at = evaluation.evaluated_at
        stored = self._rt.repos.decisions.save(decision)
        self._engine.emit(
            "qor.decision.measurement_evaluated",
            entity=stored,
            actor=actor,
            payload={
                "verdict": verdict,
                "eligibility": eligibility,
                "action_ref": evaluation.action_ref,
            },
            correlation_id=stored.task_id,
        )
        return stored

    def advance_task_to_deciding(self, task: Task) -> Task:
        if task.owner_agent_id is None:
            raise QORError(f"{task.ref} has no owner, so nobody can decide")
        owner = self._rt.repos.agents.get(task.owner_agent_id)
        return self._rt.repos.tasks.save(
            self._engine.fire(
                task,
                "synthesis_ready",
                actor=owner.as_actor(),
                reason_code="SYNTHESIS_READY",
                reason="synthesis and recommendation artifacts accepted; forming decision",
                correlation_id=task.id,
            ).entity
        )

    # ---- internals -----------------------------------------------------

    def _materialise(
        self,
        task: Task,
        proposal: DecisionProposal,
        accepted: tuple[Artifact, ...],
        actor: ActorRef,
    ) -> Decision:
        now = self._rt.clock.now()
        did, ref = self._rt.mint("DEC")
        reviews = tuple(
            r.id
            for r in self._rt.repos.reviews.all()
            if r.task_id == task.id and r.status in ("PASSED", "CONDITIONAL")
        )
        risk_level = RiskLevel.parse(proposal.risk_level)
        approval = self._approval_requirement(task, proposal, risk_level)

        decision = Decision(
            id=did,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=actor,
            updated_at=now,
            task_id=task.id,
            decision_type=DecisionType(proposal.decision_type),
            statement=proposal.statement,
            rationale=proposal.rationale,
            decided_by_agent_id=task.owner_agent_id,
            decided_by_role_id=actor.role_id,
            decided_at=now,
            based_on_artifact_ids=tuple(a.id for a in accepted),
            based_on_review_ids=reviews,
            alternatives_considered=tuple(
                Alternative(
                    option=str(alt.get("option", "")),
                    pros=_strings(alt.get("pros")),
                    cons=_strings(alt.get("cons")),
                    rejected_because=str(alt.get("rejected_because", "")),
                )
                for alt in proposal.alternatives
            ),
            measurement_plan=MeasurementPlan(
                metrics=proposal.metrics,
                evaluate_after=proposal.evaluate_after,
                success_criteria=proposal.success_criteria,
                success_checks=proposal.success_checks,
                evaluate_when=proposal.evaluate_when,
            )
            if proposal.metrics
            else None,
            confidence=proposal.confidence,
            risk_level=risk_level,
            reversibility="REVERSIBLE" if proposal.is_reversible else "IRREVERSIBLE",
            requires_approval=approval[0],
            approval_policy_ref=approval[1],
            addresses_outcome_ref=proposal.addresses_outcome_ref,
        )
        decision = self._rt.repos.decisions.insert(decision)
        self._engine.record_creation(
            decision,
            actor=actor,
            reason_code="SYNTHESIS_READY",
            reason=f"drafted from {len(accepted)} accepted artifact(s) and {len(reviews)} review(s)",
            correlation_id=task.id,
            event_name="qor.decision.created",
            payload={
                "decision_type": decision.decision_type.value,
                "statement": decision.statement,
                "requires_approval": decision.requires_approval,
            },
        )
        return decision

    def _approval_requirement(
        self, task: Task, proposal: DecisionProposal, risk_level: RiskLevel
    ) -> tuple[bool, str]:
        """Computed from policy, never taken from the proposer.

        V0.7 keeps Decision as intent: a SANDBOX dry-run action is gated at the
        Action/Task door, not by blocking ``decide``. LIVE/PAPER still mark the
        Decision as requiring approval so it cannot silently skip that machine.
        """
        if proposal.proposed_actions and all(
            Environment(action.environment) is Environment.SANDBOX
            for action in proposal.proposed_actions
        ):
            return False, ""
        worst_class = RiskClass.READ_ONLY
        worst_env = Environment.SANDBOX
        for action in proposal.proposed_actions:
            env = Environment(action.environment)
            nominal = (
                self._rt.capability(action.capability_key).risk_class
                if action.capability_key in self._rt.capability_index
                else RiskClass.READ_ONLY
            )
            effective, _ = self._rt.policies.effective_risk_class(
                nominal=nominal,
                dry_run=env is Environment.SANDBOX,
                environment=env,
            )
            worst_class = max(worst_class, effective)
            if env is not Environment.SANDBOX:
                worst_env = env

        outcome = self._rt.policies.approval_outcome(
            environment=worst_env,
            task_risk=max(task.risk_level, risk_level),
            irreversible=not proposal.is_reversible,
            capability_risk_class=worst_class,
        )
        return outcome.required, outcome.policy_ref

    def _source_outcome_ref(self, task: Task) -> str:
        if task.source_outcome_id is None:
            return ""
        source = self._rt.repos.outcomes.find(task.source_outcome_id)
        return source.ref if source is not None else ""

    def _actions_for(self, decision_id: UUID) -> tuple[Action, ...]:
        return tuple(
            item for item in self._rt.repos.actions.all() if item.decision_id == decision_id
        )

    def _eligibility(
        self,
        decision: Decision,
        plan: MeasurementPlan,
        actions: tuple[Action, ...],
        human_requested: bool,
    ) -> str:
        token = plan.evaluate_when
        if token == "AFTER_SETTLEMENT":
            if not any(item.status == "SETTLED" for item in actions):
                raise QORError(f"{decision.ref} AFTER_SETTLEMENT requires a SETTLED action")
            return "AFTER_SETTLEMENT"
        if token == "HUMAN_REQUESTED":
            if not human_requested:
                raise QORError(f"{decision.ref} HUMAN_REQUESTED requires an explicit request")
            return "HUMAN_REQUESTED"
        if not human_requested:
            raise QORError(f"{decision.ref} evaluate_after is not an executable eligibility token")
        return "HUMAN_REQUESTED"

    def _collect_observation(
        self,
        plan: MeasurementPlan,
        actions: tuple[Action, ...],
    ) -> tuple[dict[str, str], tuple[str, ...], Action | None]:
        settled = next((item for item in actions if item.status == "SETTLED"), None)
        action = settled or (actions[0] if actions else None)
        observed: dict[str, str] = {}
        sources: list[str] = []
        for check in plan.success_checks:
            value = self._observe(check, action)
            if value is None:
                continue
            observed[check.metric] = value
            if check.observation_source not in sources:
                sources.append(check.observation_source)
        if action is not None and self._world_reader is not None:
            raw = self._world_reader.read(action.target.resource)
            if raw is not None and "value" in raw and "value" not in observed:
                observed["value"] = raw["value"]
        return observed, tuple(sources), action

    def _observe(self, check: SuccessCheck, action: Action | None) -> str | None:
        if check.metric not in LEDGER_OBSERVABLE_METRICS:
            return None
        if check.observation_source == "WORLD_LEDGER":
            if self._world_reader is None:
                return None
            resource = action.target.resource if action is not None else "ledger"
            raw = self._world_reader.read(resource)
            if raw is None:
                return None
            return raw.get("value")
        if check.observation_source == "ACTION_VERIFICATION_OBSERVED":
            if action is None or action.verification is None:
                return None
            return action.verification.observed.get("value")
        return None

    @staticmethod
    def _verdict(checks: tuple[SuccessCheck, ...], observed: dict[str, str]) -> str:
        if not checks:
            return "UNCERTAIN"
        failed = False
        incomplete = False
        for check in checks:
            value = observed.get(check.metric)
            if value is None:
                incomplete = True
                continue
            compared = _compare(check.operator, value, check.threshold)
            if compared is None:
                incomplete = True
            elif not compared:
                failed = True
        if failed:
            return "FAILED"
        if incomplete:
            return "UNCERTAIN"
        return "SUCCESS"


def _compare(operator: str, left: str, right: str) -> bool | None:
    try:
        first = Decimal(left)
        second = Decimal(right)
    except InvalidOperation:
        if operator == "EQ":
            return left == right
        return None
    if operator == "EQ":
        return first == second
    if operator == "GE":
        return first >= second
    if operator == "LE":
        return first <= second
    if operator == "GT":
        return first > second
    if operator == "LT":
        return first < second
    return None
