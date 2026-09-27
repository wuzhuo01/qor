"""Shared fixtures.

Every test runs against a FrozenClock so that a failing assertion points at logic
rather than at timing. The default objective is an organizational task. The app
fixture supplies a test organization that has configured work capabilities.
The product default does not.
"""

from __future__ import annotations

import pytest

from qor.adapters.planner.stub import StubPlanner
from qor.app import QORApp, build_app
from qor.kernel.clock import FrozenClock
from qor.services.orchestrator import RunResult
from tests.contract_agents import ContractAgentRuntime
from tests.generic_staff import staffed_config


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def app(clock: FrozenClock) -> QORApp:
    """A bootstrapped test organization with the deterministic planner."""
    return build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(clock),
        clock=clock,
        config=staffed_config(),
    )


@pytest.fixture
def clean_app(clock: FrozenClock) -> QORApp:
    """Same organization. The contract agent does not weaken a first attempt."""
    return build_app(
        planner=StubPlanner(),
        agents=ContractAgentRuntime(clock),
        clock=clock,
        config=staffed_config(),
    )


@pytest.fixture
def objective() -> str:
    return "Prepare the organizational status for this cycle and state what the next cycle should keep."


@pytest.fixture
def run(app: QORApp, objective: str) -> RunResult:
    """One full pass of the organizational loop. Shared by most acceptance tests."""
    return app.orchestrator.run_objective(objective)
