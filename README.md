# QOR — AI-Native Organization Runtime

QOR is a generic runtime for coordinating AI-native organizational work through tasks, plans, capability-based assignment, execution, review, synthesis, decisions, human responsibility, actions, verification, and outcomes.

QOR provides organizational runtime primitives. Domain capabilities are supplied by the caller.

## Example

AI agents can finish a task.

Who decides?
Who reviews?
What is the basis?
What happens when a capability is missing?

This example uses StubPlanner, scripted agents, and no LLM. It is not an organization that runs itself. The runtime records the producer, the reviewer, the evidence, the decision, and the reason.

```bash
python -m examples.organization_runtime
```

FULL:

```text
Goal
 ↓
Plan
 ↓
Assignment
 ↓
Artifact
 ↓
Review
 ↓
Decision
 ↓
Outcome
```

GAP:

```text
Capability Gap
 ↓
BLOCKED
```

## Why QOR

An organization that uses AI still has to keep work accountable. Someone must be able to see what was planned, who was assigned by capability rather than by name, what was produced, whether an independent review accepted it, what was decided, and, when an action changed a world, whether that change was verified and recorded by a responsible human.

QOR is that runtime. It is not an autonomous company, an AI employee platform, or an industry agent platform.

## Core Model

```text
Task
→ Plan
→ Assignment
→ Execution
→ Artifact
→ Review
→ Synthesis
→ Decision
→ COMPLETED
```

The planner chooses capabilities and the shape of the plan. It does not choose agents. Assignment maps each required capability to a candidate that holds it.

A decision of no further action can complete the task. Review rework is not part of the proven path.

## When an Action Is Required

```text
Decision
→ Action
→ Permission / Risk / Approval
→ WorldWriter
→ Independent WorldReader
→ Verification
→ SETTLED
→ Human CIO Outcome
```

`EXECUTED` is not `SETTLED`. Settlement requires an independent reread that matches the expected world state. Only the Human CIO records an authoritative outcome, and only after that verified settlement. The system does not write that outcome by itself. A failed verification is not recorded as an outcome.

## What QOR Provides

- Task intake and a task-scoped execution lifecycle
- Plans validated against the configured capability catalog
- Capability-based assignment
- Independent review, synthesis, and decision
- Human responsibility on a waiting source, including a narrow escalation path
- Explicit notification when an active human responsibility requires action
- Governed sandbox actions, with permission, risk, and approval checks
- Independent world verification before settlement
- Scheduler and event routing that open a task
- Lesson retrieval as advice to a later plan

## What QOR Does Not Provide

- A catalog of domain business capabilities
- Automatic execution after a schedule tick or an event
- Behavioral learning from lessons
- Automatic authoritative outcomes
- Execution identity, retry, crash recovery, execution idempotency, concurrency control, workers, queues, or dispatchers
- A complete lifecycle after a task is `BLOCKED`

## Current V1.0 Evidence

The V1.0 core is frozen. Contract tests cover the organizational loop when the caller supplies work capabilities, the governed action path through settlement and a human-recorded outcome, scheduler and event-router task creation, notification, the entry guard, responsibility closure, the escalation contract, and state-machine and permission invariants.

Limits that remain:

- A business-independent organization planned by a real language model is not fully proven.
- The generic profile does not provide a domain capability catalog.
- The public example uses StubPlanner and scripted agents. It does not call a model.

Passing the contract tests does not remove those limits.

## Quick Start

QOR requires Python 3.11 or newer.

```bash
python -m pip install -e ".[dev]"
```

The default command loads the generic profile and stops before planning:

```bash
qor --objective "Prepare the organizational status for this cycle."
```

That command exits with code 2. The generic profile intentionally has no plannable business capabilities. The same entry is available as `python -m qor.cli`.

See [Getting started](docs/en/getting-started.md).

## Tests

From the repository root, the public contract tests are:

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

The last recorded result for this set is 190 passed. Do not treat a full-repository test run as the public contract. Other tests in the tree are local development material.

## Documentation

- [Getting started](docs/en/getting-started.md)
- [Concepts](docs/en/concepts.md)
- [Architecture](docs/en/architecture.md)
- [Evidence boundary](docs/en/evidence-boundary.md)
- [中文 README](README.zh-CN.md)

## Current Boundaries

The public example does not invent a domain catalog. Run `python -m examples.organization_runtime`. It uses StubPlanner and scripted agents, and it does not call a model.

The scheduler and the event router create a task in `OPEN`. They do not call task execution.

Lessons can be retrieved. They do not change the next plan and they are not binding constraints.

`ESCALATED` means only this path:

```text
AWAITING_HUMAN → ESCALATED → AWAITING_HUMAN
```

`BLOCKED` is an exceptional stop. Its later lifecycle is outside the V1.0 contract.

## License

License: Apache-2.0. See [LICENSE](LICENSE).
