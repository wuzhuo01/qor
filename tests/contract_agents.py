"""Deterministic agent port for contract tests.

This is not a product capability. It carries out the node it is given and, when
a test needs an Action, proposes one sandbox increment. It does not embed an
industry scenario.
"""

from __future__ import annotations

from qor.adapters.agents.generic import GenericAgentRuntime
from qor.kernel.clock import Clock
from qor.ports.agent_runtime import (
    ActionProposal,
    DecisionProposal,
    DecisionRequest,
)


class ContractAgentRuntime(GenericAgentRuntime):
    def __init__(self, clock: Clock, *, world_effect: bool = False) -> None:
        super().__init__(clock)
        self._world_effect = world_effect

    def propose_decision(self, agent_id, request: DecisionRequest) -> DecisionProposal:
        environment = request.permitted_environments[0]
        return DecisionProposal(
            decision_type="PARAMETER_CHANGE",
            statement="Apply one reversible sandbox update.",
            rationale=f"The accepted account selected one sandbox update. Task context: {request.task_objective}",
            alternatives=(
                {
                    "option": "Leave the sandbox unchanged",
                    "rejected_because": "the accepted account already selected a change",
                },
            ),
            metrics=("sandbox_counter",),
            evaluate_after="the next organizational cycle",
            success_criteria=("the sandbox counter matches the approved increment",),
            proposed_actions=(
                ActionProposal(
                    system="sandbox-world",
                    resource="ledger",
                    environment=environment,
                    operation="increment",
                    capability_key="sandbox.world_increment",
                    payload={"delta": 1},
                    is_reversible=True,
                    dry_run=not self._world_effect,
                ),
            ),
        )
