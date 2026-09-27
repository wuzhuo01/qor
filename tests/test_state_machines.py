"""The state machines are data, and the data must stay honest.

Declaring the machines as YAML is what makes the workflow runtime replaceable: the
organization's rules live in configuration, not in a graph library's API. That only
holds if the configuration is validated as strictly as code would be.
"""

from __future__ import annotations

import pytest
import yaml

from qor.kernel.errors import TransitionRejected
from qor.kernel.statemachine import ANY_STATE, StateMachine, StateMachineRegistry
from qor.services.config import QORConfig

pytestmark = pytest.mark.invariant

EXPECTED_ENTITIES = {
    "Task",
    "TaskPlan",
    "TaskNode",
    "TaskAssignment",
    "Artifact",
    "Review",
    "Decision",
    "Action",
    "Agent",
    "Outcome",
    "Lesson",
    "Schedule",
    "Responsibility",
}


@pytest.fixture(scope="module")
def registry() -> StateMachineRegistry:
    return StateMachineRegistry.load(QORConfig.load().state_machine_dir)


def test_every_entity_with_a_lifecycle_has_a_machine(registry):
    assert set(registry.entities()) == EXPECTED_ENTITIES


def test_every_state_is_reachable_from_the_initial_state(registry):
    """An unreachable state is either dead configuration or a missing transition.
    Both are bugs, and both are invisible without this check."""
    for entity in registry.entities():
        machine = registry.for_entity(entity)
        reachable = {machine.initial}
        changed = True
        while changed:
            changed = False
            for spec in machine.transitions.values():
                sources = set(machine.states) if ANY_STATE in spec.source else set(spec.source)
                if not (sources & reachable):
                    continue
                targets = sources if spec.returns_to_previous else {spec.target}
                for target in targets:
                    if target not in reachable:
                        reachable.add(target)
                        changed = True
        unreachable = set(machine.states) - reachable
        assert not unreachable, f"{entity}: unreachable states {sorted(unreachable)}"


def test_every_non_terminal_state_can_be_left(registry):
    """A non-terminal state with no way out is a trap: work enters and never returns,
    with no error to explain why."""
    for entity in registry.entities():
        machine = registry.for_entity(entity)
        for name, state in machine.states.items():
            if state.terminal:
                continue
            has_exit = any(
                ANY_STATE in spec.source or name in spec.source
                for spec in machine.transitions.values()
            )
            assert has_exit, f"{entity}.{name} is non-terminal but has no outgoing transition"


def test_no_transition_leaves_a_terminal_state(registry):
    """Terminal means terminal. Ruling E2 turns on this: a FAILED Review must not be
    reopened, so a new Review is created instead."""
    for entity in registry.entities():
        machine = registry.for_entity(entity)
        terminal = {name for name, state in machine.states.items() if state.terminal}
        for transition, spec in machine.transitions.items():
            if ANY_STATE in spec.source:
                # A wildcard transition must exclude terminal states explicitly.
                assert spec.excludes, (
                    f"{entity}.{transition} applies to any state but excludes nothing, "
                    "so it can reopen a terminal state"
                )
                assert terminal <= set(spec.excludes), (
                    f"{entity}.{transition} can leave terminal states "
                    f"{sorted(terminal - set(spec.excludes))}"
                )
            else:
                leaks = terminal & set(spec.source)
                assert not leaks, f"{entity}.{transition} leaves terminal state(s) {sorted(leaks)}"


def test_review_uses_running_and_never_in_progress(registry):
    """Ruling E2 fixed the vocabulary. A synonym creeping back in would split the
    intermediate state across two names and quietly break every query about it."""
    machine = registry.for_entity("Review")
    assert "RUNNING" in machine.states
    assert "IN_PROGRESS" not in machine.states
    assert "REWORK" not in machine.states, "E2 forbade a REWORK state on Review"

    assert machine.spec("start").source == ("PENDING",)
    assert machine.spec("start").target == "RUNNING"
    assert machine.states["FAILED"].terminal
    assert machine.states["PASSED"].terminal
    assert machine.states["CONDITIONAL"].terminal


def test_every_declared_guard_has_an_implementation(registry):
    """A guard named in YAML but missing in code must fail loudly at load time. If it
    were skipped, the machine would appear to enforce a rule it does not."""
    from qor.services.guards import GuardRegistry

    implemented = GuardRegistry.default().names()
    declared: set[str] = set()
    for entity in registry.entities():
        for spec in registry.for_entity(entity).transitions.values():
            declared |= set(spec.guards)

    missing = declared - implemented
    assert not missing, f"declared in YAML but not implemented: {sorted(missing)}"

    unused = implemented - declared
    assert not unused, f"implemented but never declared, so never runs: {sorted(unused)}"


def test_every_declared_permission_exists_in_the_catalog(registry):
    config = QORConfig.load()
    for entity in registry.entities():
        for transition, spec in registry.for_entity(entity).transitions.items():
            if spec.permission:
                assert spec.permission in config.known_permissions, (
                    f"{entity}.{transition} requires undeclared permission '{spec.permission}'"
                )


def test_every_emitted_event_is_namespaced(registry):
    for entity in registry.entities():
        for transition, spec in registry.for_entity(entity).transitions.items():
            if spec.emits:
                assert spec.emits.startswith("qor."), (
                    f"{entity}.{transition} emits '{spec.emits}' outside the qor namespace"
                )


def test_the_reason_code_vocabulary_is_shared_and_non_empty(registry):
    vocabularies = {registry.for_entity(e).reason_codes for e in registry.entities()}
    assert len(vocabularies) == 1, (
        "each machine has its own vocabulary, so codes are not comparable"
    )
    vocabulary = next(iter(vocabularies))
    assert len(vocabulary) > 20
    assert len(set(vocabulary)) == len(vocabulary), "the vocabulary contains duplicates"


def test_a_reason_code_outside_the_vocabulary_is_rejected(registry):
    machine = registry.for_entity("Task")
    with pytest.raises(TransitionRejected):
        machine.validate_reason("admit", "BECAUSE_I_SAID_SO", "a reason")


def test_an_empty_reason_is_rejected(registry):
    machine = registry.for_entity("Task")
    with pytest.raises(TransitionRejected):
        machine.validate_reason("admit", "HUMAN_REQUEST", "   ")


# --------------------------------------------------------------------------
# The loader's own self-check
# --------------------------------------------------------------------------


def test_a_transition_to_an_undeclared_state_is_rejected_at_load():
    bad = {
        "name": "broken",
        "entity": "Broken",
        "initial": "A",
        "states": ["A", {"name": "B", "terminal": True}],
        "transitions": [{"name": "go", "from": "A", "to": "NOWHERE"}],
    }
    with pytest.raises(ValueError, match="NOWHERE"):
        StateMachine.from_dict(bad)


def test_a_previous_transition_must_have_exactly_one_source():
    """`$previous` means "go back where you came from". From two possible sources the
    answer is ambiguous, so the configuration is rejected rather than guessed."""
    bad = {
        "name": "broken",
        "entity": "Broken",
        "initial": "A",
        "states": ["A", "B", "C"],
        "transitions": [{"name": "back", "from": ["B", "C"], "to": "$previous"}],
    }
    with pytest.raises(ValueError):
        StateMachine.from_dict(bad)


def test_the_shipped_configuration_loads_and_is_self_consistent(registry):
    """The load itself runs every self-check, so this is the regression test for the
    configuration as shipped."""
    counts = {
        entity: (
            len(registry.for_entity(entity).states),
            len(registry.for_entity(entity).transitions),
        )
        for entity in registry.entities()
    }
    for entity, (states, transitions) in counts.items():
        # Schedule is the approved binary ENABLED/DISABLED rule, not a stub machine.
        minimum_states = 2 if entity == "Schedule" else 3
        assert states >= minimum_states, f"{entity} has only {states} states"
        assert transitions >= states - 1, f"{entity} has fewer transitions than states"


def test_yaml_files_declare_exactly_one_machine_each():
    directory = QORConfig.load().state_machine_dir
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "reason_codes.yaml":
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert {"name", "entity", "initial", "states", "transitions"} <= set(raw)
        assert raw["initial"] in [
            item if isinstance(item, str) else item["name"] for item in raw["states"]
        ], f"{path.name}: initial state is not in the state list"
