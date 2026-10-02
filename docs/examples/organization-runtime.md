# Organization runtime example

This is the public QOR example. It shows governance, not an organization that runs itself.

AI agents can finish a task. Who decides? Who reviews? What is the basis? What happens when a capability is missing?

```bash
python -m examples.organization_runtime
```

This example uses:

- StubPlanner
- scripted agents
- no LLM

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

The Analyst is the producer. The Reviewer reviews. Evidence kinds are `external`, `tool`, and `reasoning`. A fact cited only as reasoning fails review. The CIO decision records its reason. The outcome stays `UNCERTAIN` because the next stage has not been observed.

GAP:

```text
Capability Gap
 ↓
BLOCKED
```

`organization.unassigned_check` is planned and nobody holds it. The task stops with reason `CAPABILITY_GAP`. There is no decision.
