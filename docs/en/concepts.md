# Concepts

These are the objects QOR uses to keep organizational work accountable. Names below are the runtime's own terms.

## Organization

An organization is the running environment of capabilities, roles, and responsibility. Agents hold capabilities. The runtime assigns work to agents that hold the required capability. The generic organization is an entry shell. It configures governance roles and does not contain plannable domain work.

## Task

A task is the lifecycle of one organizational objective. Its status is the execution lifecycle. Opening a task does not by itself execute it.

## Plan

A plan is the directed structure of the work: nodes, the capabilities they require, and their dependencies. The planner proposes it. The runtime validates it against the catalog and policy before anything is assigned.

## Capability

A capability is a named kind of work the organization has configured. The planner selects capabilities. It does not select agents, and a plan request does not carry an agent identity.

Capabilities used to govern the organization, such as planning, review, and decision, are not themselves the plannable business work. Business capabilities are whatever the caller configures. The generic profile configures none.

## Assignment

Assignment maps a node's required capabilities to a candidate agent that holds them. If nobody holds the capability, the work cannot be assigned by naming a substitute.

## Execution

Execution carries out an assigned node and produces an artifact. The artifact is the record of that work, not a claim that the task is finished.

## Review

Review is an independent gate. It is a real review object. The agent that produced the artifact does not declare the review passed.

The proven runs passed review. Rework after a failed review is not part of that proven path.

## Synthesis

Synthesis integrates accepted upstream work into one account. A synthesis node has to depend on upstream nodes. It is not a separate agent chosen by name.

## Decision

A decision is a commitment. It is separate from an action. A decision can be to take no further action, in which case the task can complete. A decision that proposes an action still does not perform that action.

## Human CIO

The Human CIO is the person who records an authoritative outcome. The runtime does not write that record by itself. Approval, denial, and the narrow escalation path are also human actions.

## Action

An action is a proposed operation on a world. Before it proceeds it passes permission, risk, and approval checks. A sandbox dry run can stop under policy without touching the world. Not every decision creates an action.

## Verification

After a world writer applies a change, an independent world reader rereads the world. The comparison between the expected state and that reread is the verification. The writer's return value is not the verification result.

## SETTLED

`EXECUTED` is not `SETTLED`. An action is settled only after verification matches. A mismatch does not become an authoritative outcome.

## Outcome

An authoritative outcome exists only when a settled action has a matching verification and the Human CIO records it. That fact can appear on the next plan request. A confirmed outcome is not, by itself, a binding constraint. A high-confidence contradicted outcome can become one. Failed verification is not recorded as an outcome.

## Lesson

A lesson can be retrieved and shown to a later plan as advice. Retrieval does not change the plan. A lesson is not a binding constraint, and QOR does not apply lessons as behavioral learning.
