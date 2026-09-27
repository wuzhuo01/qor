# Architecture

This page describes the V1.0 runtime as it behaves now. It is not a roadmap.

## Runtime flow

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

When a decision requires an action:

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

A decision of no further action completes without that second path. `EXECUTED` is not `SETTLED`.

## Capability-based assignment

The planner receives a catalog of capabilities and returns a plan that names capabilities, dependencies, and expected artifacts. It does not name agents. Assignment then selects a candidate that holds the required capabilities.

The generic profile's governance capabilities are organizational machinery. They are excluded from the plannable work list because they carry required permissions. A caller adds work capabilities in configuration. QOR does not invent them so that a demonstration can run.

## Human responsibility

`AWAITING_HUMAN` means the task is waiting for a person. An active responsibility can be bound to that source. Notification can record that the responsible person has something to do. Creating or transferring that responsibility can create the notification. Clearing or completing it does not. A failed delivery does not roll back the responsibility or the source.

`ESCALATED` has only this meaning:

```text
AWAITING_HUMAN → ESCALATED → AWAITING_HUMAN
```

It is not a general failure state and it is not entered from other task states.

`BLOCKED` is an exceptional stop. The V1.0 contract does not define what happens after entry, and it does not imply retry, recovery, or automatic escalation.

## Reliability model

What exists:

- An entry guard checks the stored task state once before execution.
- The task status is the execution lifecycle. There is no separate execution object.
- `OPEN` allows the first execution.
- `READY` with an active plan allows resume of that plan.

What does not exist, and is not a promise of a later release on this page:

- `execution_id`
- retry
- crash recovery
- execution idempotency
- concurrency control
- worker
- queue
- dispatcher

This is the V1.0 reliability contract. It is not a complete reliability system.

## Scheduler and event router

A due schedule opens one task with origin `SCHEDULE` and status `OPEN`. A matching event opens one task with origin `EVENT` and status `OPEN`.

Scheduler and event router can create a task in `OPEN`. They do not automatically execute the task. They do not call `run_task`. A resolved routing event is not an execution trigger.

## Evidence boundaries

Proven for the V1.0 core, with the limits already stated: the organizational loop when work capabilities are supplied, the action path through independent verification and a Human CIO outcome, dogfooding of that loop, scheduler and event-router task creation, notification, the entry guard, responsibility closure, the escalation contract, and state-machine and permission invariants.

Not fully proven: business-independent planning by a real language model. Not provided: a generic domain capability catalog, and a public neutral business example.

Lessons are retrieval only. The system does not record authoritative outcomes automatically.

## Legacy enum values

Some legacy enum values remain for V1.0 compatibility. They do not define QOR's product domain or generic capability catalog.
