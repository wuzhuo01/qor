"""Invariants that hold regardless of which task is running.

These are the properties that make the runtime a governed organization rather than a
script that happens to produce artifacts. Each test names the invariant it protects,
so a future failure says which architectural guarantee was lost.
"""

from __future__ import annotations

import pytest

from qor.domain.enums import Environment, RiskClass, TaskOrigin
from qor.kernel.actor import ActorKind, ActorRef
from qor.kernel.errors import AuthorizationError, IllegalStateWrite, QORError, TransitionRejected

pytestmark = pytest.mark.invariant


# --------------------------------------------------------------------------
# I26  No AI agent holds a Human-CIO-exclusive permission
# --------------------------------------------------------------------------


def test_i26_no_ai_agent_holds_a_human_exclusive_permission(app):
    """Checked at bootstrap, and re-checked here so a config edit cannot slip past.

    The concrete danger this prevents: an AI holding `capability.risk_class.modify`
    could reclassify a REALITY_AFFECTING capability as READ_ONLY and route a
    reality-affecting action around the Action Gateway entirely, without ever violating another rule.
    """
    app.authorizer.verify_no_ai_holds_exclusive_permissions()

    exclusive = set(app.runtime.config.human_cio_exclusive)
    assert exclusive, "the exclusive permission list is empty, so nothing is reserved"

    for agent in app.runtime.repos.agents.all():
        if agent.kind is not ActorKind.HUMAN:
            granted = app.authorizer.effective_permissions(agent)
            overlap = granted & exclusive
            assert not overlap, f"AI agent {agent.name} holds human-only {sorted(overlap)}"


def test_i26_at_least_one_human_can_actually_exercise_the_reserved_powers(app):
    """The mirror image: reserving a power that nobody holds is not governance, it is
    a dead letter. Somebody must be able to say yes."""
    holders = [
        agent
        for agent in app.runtime.repos.agents.all()
        if agent.kind is ActorKind.HUMAN
        and set(app.runtime.config.human_cio_exclusive)
        <= app.authorizer.effective_permissions(agent)
    ]
    assert holders, "no human holds the reserved permissions, so they can never be used"


def test_i26_a_machine_actor_can_never_approve(app):
    """SYSTEM and RULE may move state but are not accountable principals."""
    for kind in (ActorKind.SYSTEM, ActorKind.RULE):
        actor = ActorRef(kind=kind, display=f"{kind}-TEST")
        assert actor.is_machine_authority
        for permission in ("approval.grant.live_capital", "approval.grant.irreversible"):
            with pytest.raises(AuthorizationError):
                app.authorizer.check(
                    actor=actor,
                    permission=permission,
                    target_type="Decision",
                    target_id=app.runtime.organization_id,
                    target_ref="TEST",
                )


def test_an_undeclared_permission_is_denied_rather_than_waved_through(app):
    """A misspelled permission must fail closed. If it failed open, a typo in a guard
    would look exactly like a check that passed."""
    with pytest.raises(AuthorizationError) as caught:
        app.authorizer.check(
            actor=ActorRef.system("TEST"),
            permission="action.exceute",  # deliberate typo
            target_type="Action",
            target_id=app.runtime.organization_id,
            target_ref="TEST",
        )
    assert "not a declared permission" in str(caught.value)


# --------------------------------------------------------------------------
# Three-layer permission narrowing
# --------------------------------------------------------------------------


def test_permissions_narrow_and_never_widen(app, run):
    """An Assignment's scope is a subset of the agent's effective permissions.

    granted = union(roles) - agent.denied - assignment.scope. If a scope could add a
    permission, delegation would become a privilege escalation path.
    """
    for assignment in app.runtime.repos.assignments.all():
        agent = app.runtime.repos.agents.get(assignment.assignee_agent_id)
        effective = app.authorizer.effective_permissions(agent)
        assert set(assignment.granted_permissions) <= effective, (
            f"{assignment.ref} granted {set(assignment.granted_permissions) - effective} "
            f"which {agent.name} does not hold"
        )


def test_an_explicit_denial_beats_a_role_grant(app):
    """The deny layer must be able to override a grant, or it is decoration."""
    agent = next(a for a in app.runtime.repos.agents.all() if a.kind is not ActorKind.HUMAN)
    permission = next(iter(app.authorizer.effective_permissions(agent)))

    agent.denied_permissions = (*agent.denied_permissions, permission)
    app.runtime.repos.agents.save(agent)

    assert permission not in app.authorizer.effective_permissions(agent)
    with pytest.raises(AuthorizationError):
        app.authorizer.check(
            actor=agent.as_actor(),
            permission=permission,
            target_type="Organization",
            target_id=app.runtime.organization_id,
            target_ref="ORG",
        )


# --------------------------------------------------------------------------
# I7  A status change requires a StateTransition
# --------------------------------------------------------------------------


def test_i7_a_bare_status_write_is_rejected_for_every_tracked_entity(app, run):
    repos_and_targets = [
        (app.runtime.repos.tasks, run.task.id, "CANCELLED"),
        (app.runtime.repos.artifacts, run.artifacts[0].id, "REJECTED"),
        (app.runtime.repos.reviews, run.reviews[0].id, "FAILED"),
        (app.runtime.repos.decisions, run.decision.id, "SUPERSEDED"),
        (app.runtime.repos.actions, run.actions[0].id, "EXECUTED"),
    ]
    for repo, entity_id, forged in repos_and_targets:
        entity = repo.get(entity_id)
        entity.status = forged
        with pytest.raises(IllegalStateWrite):
            repo.save(entity)


def test_i7_a_non_status_field_can_still_be_saved_normally(app, run):
    """The check must be narrow. If it blocked ordinary edits it would be unusable."""
    task = app.runtime.repos.tasks.get(run.task.id)
    task.labels = {**task.labels, "reviewed_by_ops": "yes"}
    saved = app.runtime.repos.tasks.save(task)
    assert saved.labels["reviewed_by_ops"] == "yes"
    assert saved.status == run.task.status


def test_optimistic_locking_rejects_a_stale_write(app, run):
    """Two writers, one wins. The loser is told, rather than silently overwriting."""
    first = app.runtime.repos.tasks.get(run.task.id)
    second = app.runtime.repos.tasks.get(run.task.id)

    first.labels = {**first.labels, "writer": "first"}
    app.runtime.repos.tasks.save(first)

    second.labels = {**second.labels, "writer": "second"}
    from qor.kernel.errors import QORError

    with pytest.raises(QORError):
        app.runtime.repos.tasks.save(second)


# --------------------------------------------------------------------------
# The Action Gateway is the only exit to reality
# --------------------------------------------------------------------------


def test_a_dry_run_action_can_never_reach_executing(app, run):
    """Ruling E1, enforced mechanically rather than by the gateway's good behaviour.

    The `execute` transition carries the `action_is_not_dry_run` guard, so even a bug
    in the gateway cannot open the door. This test asserts the guard's presence and
    its effect.
    """
    machine = app.runtime.machines.for_entity("Action")
    spec = machine.spec("execute")
    assert "action_is_not_dry_run" in spec.guards
    assert spec.permission == "action.execute.sandbox"

    action = run.actions[0]
    assert action.dry_run is True
    passed, outcomes = app.engine.evaluate(action, "execute")
    assert passed is False
    blocker = next(o for o in outcomes if o.name == "action_is_not_dry_run")
    assert blocker.passed is False
    assert "dry_run" in blocker.detail

    with pytest.raises((TransitionRejected, AuthorizationError)):
        app.engine.fire(
            action,
            "execute",
            actor=app.runtime.agent(app.runtime.config.organization["human_cio"]).as_actor(),
            reason_code="ROUTINE_PROGRESSION",
            reason="attempting to execute a dry run",
        )


def test_in_v0_1_nobody_at_all_holds_action_execute(app):
    """Reality is unreachable by construction, not only by guard.

    V0.1 declares that no real side effect may occur. That is implemented twice: the
    `action_is_not_dry_run` guard refuses the transition, and separately no role in
    `permissions.yaml` grants `action.execute` to anyone, human included. Either alone
    would be enough; both together mean a single edit cannot open the door.
    """
    for agent in app.runtime.repos.agents.all():
        granted = app.authorizer.effective_permissions(agent)
        assert "action.execute" not in granted, (
            f"{agent.name} can execute actions, which V0.1 forbids outright"
        )


def test_the_dry_run_stop_state_is_policy_not_an_invented_state(app):
    """Ruling E1 forbade a DRY_RUN state. The stop point is configuration."""
    machine = app.runtime.machines.for_entity("Action")
    assert "DRY_RUN" not in machine.states
    assert machine.stop_states["dry_run"], "no dry-run stop state is configured"
    assert "APPROVED" in machine.stop_states["dry_run"]
    assert "EXECUTING" not in machine.stop_states["dry_run"]
    # The live path still goes all the way, so this is a policy narrowing rather
    # than a capability that was removed.
    assert "SETTLED" in machine.stop_states["live"]


def test_every_action_check_records_the_policy_it_applied(app, run):
    """A check with no policy reference cannot be audited: nobody can ask whether the
    rule that was applied is the rule that should have been applied."""
    for action in run.actions:
        kinds = {c.kind for c in action.checks}
        assert {"PERMISSION", "RISK", "APPROVAL"} <= kinds, (
            f"{action.ref} skipped a gateway check: has {sorted(kinds)}"
        )
        for check in action.checks:
            assert check.policy_ref.strip(), f"{action.ref}/{check.kind} cites no policy"
            assert check.detail.strip()
            assert check.verdict in ("PASSED", "FAILED", "WAIVED")


def test_a_waived_approval_must_name_the_policy_that_waived_it(app, run):
    """Approval is out of scope for V0.1, so the check returns WAIVED. It must say
    under what authority, or "out of scope" becomes indistinguishable from "skipped"."""
    for action in run.actions:
        for check in action.checks:
            if check.verdict == "WAIVED":
                assert check.policy_ref.strip()
                assert "POLICY" in check.policy_ref.upper()


def test_the_effective_risk_class_reduction_has_one_implementation(app):
    """This was a real bug: the gateway and the decision service each reduced the risk
    class for dry runs, and the two copies disagreed, so a decision demanded an
    approval the gateway had already waived."""
    policies = app.runtime.policies
    nominal = RiskClass.REALITY_AFFECTING

    reduced, basis = policies.effective_risk_class(
        nominal=nominal, dry_run=True, environment=Environment.SANDBOX
    )
    assert reduced < nominal
    assert basis.strip()

    unchanged, basis = policies.effective_risk_class(
        nominal=nominal, dry_run=False, environment=Environment.LIVE
    )
    assert unchanged is nominal
    assert basis.strip()

    # A dry run outside the sandbox is not reduced: the environment is what makes it
    # harmless, not the flag.
    still_real, _ = policies.effective_risk_class(
        nominal=nominal, dry_run=True, environment=Environment.LIVE
    )
    assert still_real is nominal


# --------------------------------------------------------------------------
# The three logs stay orthogonal
# --------------------------------------------------------------------------


def test_state_transitions_and_events_are_not_the_same_log(app, run):
    """StateTransition answers "what changed and why". Event answers "what should the
    organization react to". Collapsing them would mean every internal step becomes a
    broadcast, or every broadcast requires a state change."""
    transitions = app.runtime.log.transitions()
    events = app.runtime.log.events()
    assert transitions and events

    for event in events:
        assert event.name.startswith("qor."), f"event '{event.name}' is outside the namespace"
        assert event.category is not None
        assert event.occurred_at is not None

    # Bootstrap emits before any task exists, so those events have no correlation.
    # Everything emitted while a task was running must be traceable to it.
    correlated = [e for e in events if e.correlation_id is not None]
    assert correlated, "no event was correlated to the work that caused it"
    assert {e.correlation_id for e in correlated} <= {
        t.id for t in app.runtime.repos.tasks.all()
    }, "an event is correlated to something that is not a task"


def test_non_human_submit_is_not_labeled_event(app):
    """submit() is the HUMAN door. A non-human requester must not become EVENT."""
    agent = next(
        item for item in app.runtime.repos.agents.all() if item.kind is not ActorKind.HUMAN
    )
    before = len(app.runtime.repos.tasks.all())
    with pytest.raises(QORError, match="not TaskOrigin.EVENT"):
        app.orchestrator.intake.submit(
            objective="Record the weekly organizational status.",
            requested_by_agent_id=agent.id,
        )
    assert len(app.runtime.repos.tasks.all()) == before
    assert TaskOrigin.EVENT not in {task.origin for task in app.runtime.repos.tasks.all()}


def test_no_event_causes_a_task_to_be_created(app, run):
    """V0.1 implements Event Record and Emit, but explicitly not Event -> Task
    triggering. If an event had created work, the declared scope would be wrong."""
    origins = {task.origin for task in app.runtime.repos.tasks.all()}
    assert TaskOrigin.EVENT not in origins


def test_audit_records_are_hash_chained_and_tamper_evident(app, run):
    assert app.runtime.log.verify_audit_chain()

    records = app.runtime.log.audit_records()
    assert len(records) >= 2
    for previous, current in zip(records, records[1:], strict=False):
        assert current.prev_hash == previous.record_hash, "the chain is not linked"
        assert current.seq > previous.seq

    # Tampering with a record must invalidate the chain, or the chain proves nothing.
    victim = records[len(records) // 2]
    original = victim.permission
    object.__setattr__(victim, "permission", "action.execute")
    assert not app.runtime.log.verify_audit_chain()
    object.__setattr__(victim, "permission", original)
    assert app.runtime.log.verify_audit_chain()


def test_every_audit_record_names_an_accountable_actor(app, run):
    for record in app.runtime.log.audit_records():
        assert record.actor is not None
        assert record.permission
        assert record.target_ref
        assert record.basis.strip(), "an audit record with no basis explains nothing"


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_same_inputs_produce_the_same_run(clock, objective):
    """Not a nicety: without it, a failing acceptance test cannot be reproduced, and
    the audit log cannot be compared between runs."""
    from qor.adapters.planner.stub import StubPlanner
    from qor.app import build_app
    from qor.kernel.clock import FrozenClock
    from tests.contract_agents import ContractAgentRuntime
    from tests.generic_staff import staffed_config

    def fingerprint() -> tuple:
        the_clock = FrozenClock()
        app = build_app(
            planner=StubPlanner(),
            agents=ContractAgentRuntime(the_clock),
            clock=the_clock,
            config=staffed_config(),
        )
        result = app.orchestrator.run_objective(objective)
        return (
            result.task.status,
            tuple(a.ref for a in result.artifacts),
            tuple((r.ref, r.round, r.verdict) for r in result.reviews),
            tuple(
                (t.entity_ref, t.from_state, t.to_state, t.reason_code)
                for t in app.runtime.log.transitions()
            ),
        )

    assert fingerprint() == fingerprint()
