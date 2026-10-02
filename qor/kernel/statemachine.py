"""Declarative state machine engine.

State machines are data (``qor/config/state_machines/*.yaml``), never code. This is
what makes the workflow runtime replaceable: the rules live here, and LangGraph --
or any future runtime -- only decides *when* to attempt a transition, never
*whether* it is legal.

The engine itself is pure: it validates and computes. Persisting the new state,
writing the StateTransition and emitting the Event happen in
``qor.services.transitions``, which is the single choke point for I7 and I8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from qor.kernel.errors import TransitionRejected, UnknownTransition

__all__ = [
    "GuardOutcome",
    "StateMachine",
    "StateMachineRegistry",
    "StateSpec",
    "TransitionSpec",
]

ANY_STATE = "*"


@dataclass(frozen=True)
class GuardOutcome:
    """Result of one guard evaluation. Recorded into StateTransition.guard_results."""

    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"guard": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class StateSpec:
    name: str
    terminal: bool = False
    exceptional: bool = False
    description: str = ""


@dataclass(frozen=True)
class TransitionSpec:
    name: str
    source: tuple[str, ...]
    target: str
    guards: tuple[str, ...] = ()
    emits: str = ""
    permission: str | None = None
    reason_required: bool = True
    returns_to_previous: bool = False
    description: str = ""

    def accepts(self, current: str) -> bool:
        return ANY_STATE in self.source or current in self.source


@dataclass(frozen=True)
class StateMachine:
    name: str
    entity: str
    initial: str
    states: dict[str, StateSpec]
    transitions: dict[str, TransitionSpec]
    reason_codes: tuple[str, ...] = ()
    stop_states: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # ---- queries -------------------------------------------------------

    def spec(self, transition: str) -> TransitionSpec:
        try:
            return self.transitions[transition]
        except KeyError:
            raise UnknownTransition(
                f"{self.name} has no transition '{transition}'; known: {sorted(self.transitions)}"
            ) from None

    def is_terminal(self, state: str) -> bool:
        return self.states[state].terminal

    def available(self, current: str) -> list[TransitionSpec]:
        if self.is_terminal(current):
            return []
        return [t for t in self.transitions.values() if t.accepts(current)]

    def resolve(self, transition: str, current: str) -> TransitionSpec:
        """Validate that ``transition`` may be attempted from ``current``.

        Raises rather than returning a bool: an illegal transition attempt is a
        defect, and callers should not be able to ignore it.
        """
        spec = self.spec(transition)
        if self.is_terminal(current):
            raise TransitionRejected(
                self.name, transition, f"'{current}' is terminal and cannot be left"
            )
        if not spec.accepts(current):
            raise TransitionRejected(
                self.name,
                transition,
                f"not allowed from '{current}' (allowed: {list(spec.source)})",
            )
        if spec.target not in self.states and not spec.returns_to_previous:
            raise TransitionRejected(
                self.name, transition, f"target '{spec.target}' is not a declared state"
            )
        return spec

    def validate_reason(self, transition: str, reason_code: str, reason: str) -> None:
        """I-ST2: reason is mandatory and reason_code must be in the controlled list."""
        spec = self.spec(transition)
        if spec.reason_required and not reason.strip():
            raise TransitionRejected(self.name, transition, "reason is required and was empty")
        if self.reason_codes and reason_code not in self.reason_codes:
            raise TransitionRejected(
                self.name,
                transition,
                f"reason_code '{reason_code}' not in controlled vocabulary",
            )

    # ---- loading -------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, reason_codes: tuple[str, ...] = ()) -> StateMachine:
        states = {}
        for item in raw["states"]:
            if isinstance(item, str):
                states[item] = StateSpec(name=item)
            else:
                name = item["name"]
                states[name] = StateSpec(
                    name=name,
                    terminal=bool(item.get("terminal", False)),
                    exceptional=bool(item.get("exceptional", False)),
                    description=item.get("description", ""),
                )

        transitions: dict[str, TransitionSpec] = {}
        for item in raw["transitions"]:
            source = item["from"]
            sources = tuple(source) if isinstance(source, list) else (source,)
            target = item["to"]
            spec = TransitionSpec(
                name=item["name"],
                source=sources,
                target=target,
                guards=tuple(item.get("guards", ())),
                emits=item.get("emits", ""),
                permission=item.get("permission"),
                reason_required=bool(item.get("reason_required", True)),
                returns_to_previous=target == "$previous",
                description=item.get("description", ""),
            )
            if spec.name in transitions:
                raise ValueError(f"{raw['name']}: duplicate transition '{spec.name}'")
            transitions[spec.name] = spec

        machine = cls(
            name=raw["name"],
            entity=raw["entity"],
            initial=raw["initial"],
            states=states,
            transitions=transitions,
            reason_codes=reason_codes,
            stop_states={k: tuple(v) for k, v in (raw.get("stop_states") or {}).items()},
        )
        machine._self_check()
        return machine

    def _self_check(self) -> None:
        if self.initial not in self.states:
            raise ValueError(f"{self.name}: initial '{self.initial}' is not a declared state")
        for spec in self.transitions.values():
            for src in spec.source:
                if src != ANY_STATE and src not in self.states:
                    raise ValueError(
                        f"{self.name}.{spec.name}: source '{src}' is not a declared state"
                    )
            if spec.returns_to_previous:
                # A $previous transition must leave from a single state, otherwise
                # "where do we go back to" has no well-defined answer.
                if len(spec.source) != 1 or ANY_STATE in spec.source:
                    raise ValueError(
                        f"{self.name}.{spec.name}: a $previous transition needs exactly one "
                        f"concrete source state, got {list(spec.source)}"
                    )
                continue
            if spec.target not in self.states:
                raise ValueError(
                    f"{self.name}.{spec.name}: target '{spec.target}' is not a declared state"
                )
            if self.states[spec.target].terminal and spec.target in spec.source:
                raise ValueError(
                    f"{self.name}.{spec.name}: cannot transition out of terminal '{spec.target}'"
                )


class StateMachineRegistry:
    """Loads every machine once and answers lookups by entity type."""

    def __init__(self, machines: dict[str, StateMachine]) -> None:
        self._by_entity = machines

    def for_entity(self, entity: str) -> StateMachine:
        try:
            return self._by_entity[entity]
        except KeyError:
            raise UnknownTransition(
                f"no state machine registered for entity '{entity}'; "
                f"known: {sorted(self._by_entity)}"
            ) from None

    def entities(self) -> list[str]:
        return sorted(self._by_entity)

    @classmethod
    def load(cls, directory: Path) -> StateMachineRegistry:
        reason_codes: tuple[str, ...] = ()
        vocab = directory / "reason_codes.yaml"
        if vocab.exists():
            data = yaml.safe_load(vocab.read_text(encoding="utf-8")) or {}
            reason_codes = tuple(data.get("reason_codes", ()))

        machines: dict[str, StateMachine] = {}
        for path in sorted(directory.glob("*.yaml")):
            if path.name == "reason_codes.yaml":
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            machine = StateMachine.from_dict(raw, reason_codes=reason_codes)
            if machine.entity in machines:
                raise ValueError(f"duplicate state machine for entity '{machine.entity}'")
            machines[machine.entity] = machine
        if not machines:
            raise ValueError(f"no state machines found in {directory}")
        return cls(machines)
