"""The default CLI entry is the generic organization."""

from __future__ import annotations

import pytest

from qor.cli import main as cli_main
from qor.services.config import QORConfig

OBJECTIVE = "Prepare a routine organizational improvement plan for the next operating cycle."


def test_default_profile_has_no_business_catalog():
    config = QORConfig.load()
    keys = {item["key"] for item in config.capabilities["capabilities"]}
    assert keys == {
        "governance.review",
        "governance.planning",
        "governance.decision",
        "sandbox.world_increment",
    }
    assert config.plannable_capability_keys() == ()
    assert config.organization["organization"]["name"] == "Organization"


def test_default_cli_does_not_load_example_or_call_planner(monkeypatch, capsys):
    def _forbidden() -> None:
        raise AssertionError("default entry must not resolve a planner without work capabilities")

    monkeypatch.setattr("qor.app.from_environment", _forbidden)
    code = cli_main(["--objective", OBJECTIVE])
    assert code == 2
    err = capsys.readouterr().err
    assert "no plannable work capabilities" in err
    assert "business catalog" in err


def test_cli_requires_an_objective():
    with pytest.raises(SystemExit) as raised:
        cli_main([])
    assert raised.value.code == 2
