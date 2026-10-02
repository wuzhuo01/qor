# Getting started

QOR is a generic runtime for coordinating AI-native organizational work. Domain capabilities are supplied by the caller.

## Installation

QOR requires Python 3.11 or newer. From the repository root:

```bash
python -m pip install -e ".[dev]"
```

The runtime depends on Pydantic and PyYAML. The `dev` extra adds pytest, Ruff, and mypy. After installation, the `qor` command is available. `python -m qor.cli` is the same entry.

## First run

The default profile is `generic`. It intentionally does not contain domain business capabilities, so it has nothing a planner is allowed to turn into work.

```bash
qor --objective "Prepare the organizational status for this cycle."
```

The process prints an error saying this profile has no plannable work capabilities, and exits with code 2. It does not call a planner and it does not invent a business catalog to make the command succeed.

`--objective` is required. The default planner is `llm`. That planner is reached only after a profile declares plannable work capabilities. If it is reached without a configured provider, it fails and does not fall back to a scripted plan. Provider settings are read from the environment names `QOR_LLM_BASE_URL`, `QOR_LLM_API_KEY`, and `QOR_LLM_MODEL`. Do not commit those values.

The `qor` command does not accept an extra business catalog. A complete loop in the contract tests is started by passing a caller-supplied work configuration to the application builder. That configuration is not the generic product catalog, and QOR does not ship one.

## Running tests

Run the public contract tests from the repository root:

```bash
python -m pytest \
  tests/test_cli_generic_entry.py \
  tests/test_core_organization_loop_acceptance.py \
  tests/test_core_organization_loop_action_outcome_acceptance.py \
  tests/test_qor_dogfooding_acceptance.py \
  tests/test_scheduler_acceptance.py \
  tests/test_event_router_acceptance.py \
  tests/test_notification_acceptance.py \
  tests/test_notification_auto_trigger_acceptance.py \
  tests/test_entry_guard_acceptance.py \
  tests/test_human_responsibility_acceptance.py \
  tests/test_task_escalated_contract_acceptance.py \
  tests/test_state_machines.py \
  tests/test_invariants.py
```

These tests use the real task, plan, assignment, review, decision, and, where the scenario needs it, action services. Where a scenario needs work capabilities, the test supplies that configuration. It is not the generic product catalog.

Do not treat every file under `tests/` as part of this public set.

## What success means

Success for this entry is:

- the runtime starts
- the generic configuration loads
- the command stops with code 2 because there is no plannable business work
- the public contract tests pass when a caller-supplied work configuration is present

Success is not a completed business task on the generic profile. That profile is an entry shell on purpose.
