"""Execution -- run an assignment and turn its result into an Artifact.

The agent runtime returns findings that reference evidence by *locator*, not by id.
QOR resolves those locators to EvidenceItem ids here, which means an agent cannot
mint identifiers or cite evidence it did not attach: the resolution simply fails.
"""

from __future__ import annotations

from uuid import UUID

from qor.domain.artifact import Artifact, Finding
from qor.domain.task import NodeKind, Task, TaskAssignment, TaskNode
from qor.kernel.actor import ActorRef
from qor.kernel.errors import InvariantViolation, QORError
from qor.ports.agent_runtime import AgentRuntimePort, WorkRequest, WorkResult
from qor.services.authorization import Authorizer
from qor.services.delegation import DelegationService
from qor.services.runtime import Runtime
from qor.services.transitions import TransitionEngine

__all__ = ["ExecutionService"]


class ExecutionService:
    def __init__(
        self,
        runtime: Runtime,
        engine: TransitionEngine,
        authorizer: Authorizer,
        agents: AgentRuntimePort,
        delegation: DelegationService,
    ) -> None:
        self._rt = runtime
        self._engine = engine
        self._authz = authorizer
        self._agents = agents
        self._delegation = delegation

    def execute(
        self, task: Task, node: TaskNode, assignment: TaskAssignment
    ) -> tuple[TaskNode, TaskAssignment, Artifact]:
        agent = self._rt.repos.agents.get(assignment.assignee_agent_id)
        actor = agent.as_actor(assignment.assignee_role_id)

        assignment = self._rt.repos.assignments.save(
            self._engine.fire(
                assignment,
                "start",
                actor=actor,
                reason_code="ROUTINE_PROGRESSION",
                reason=f"{agent.name} started work on {node.ref}",
                correlation_id=task.id,
            ).entity
        )
        # On rework the node is already RUNNING (the `rework` transition put it there),
        # so only a first attempt needs ASSIGNED -> RUNNING.
        if node.status == "ASSIGNED":
            node = self._rt.repos.nodes.save(
                self._engine.fire(
                    node,
                    "start",
                    actor=actor,
                    reason_code="ROUTINE_PROGRESSION",
                    reason=f"attempt {node.attempt} of {node.max_attempts} underway",
                    correlation_id=task.id,
                ).entity
            )

        # Record on the node which upstream artifacts this attempt was given, before
        # handing them over. The artifact's lineage is copied from here, so leaving it
        # unrecorded would make a synthesis unable to say what it synthesised.
        inputs = self._input_artifacts(task, node)
        node.input_artifact_ids = tuple(a.id for a in inputs)
        node = self._rt.repos.nodes.save(node)

        result = self._agents.execute_work(
            agent.id,
            WorkRequest(
                task_ref=task.ref,
                task_objective=task.objective,
                node_ref=node.ref,
                node_name=node.name,
                node_objective=node.objective,
                required_capabilities=node.required_capabilities,
                expected_artifact_type=node.expected_artifact_type,
                input_artifacts=inputs,
                attempt=node.attempt,
                required_changes=self._required_changes(node),
                scope=assignment.scope,
            ),
        )

        artifact = self._build_artifact(task, node, assignment, result, actor)
        artifact = self._submit_artifact(task, artifact, actor)

        assignment = self._rt.repos.assignments.get(assignment.id)
        assignment.result_artifact_ids = (*assignment.result_artifact_ids, artifact.id)
        assignment.cost_units += result.cost_units
        assignment = self._rt.repos.assignments.save(assignment)
        assignment = self._rt.repos.assignments.save(
            self._engine.fire(
                assignment,
                "submit",
                actor=actor,
                reason_code="WORK_SUBMITTED",
                reason=f"{agent.name} submitted {artifact.ref}",
                correlation_id=task.id,
            ).entity
        )

        node = self._rt.repos.nodes.get(node.id)
        node.output_artifact_ids = (*node.output_artifact_ids, artifact.id)
        node = self._rt.repos.nodes.save(node)
        node = self._rt.repos.nodes.save(
            self._engine.fire(
                node,
                "submit",
                actor=actor,
                reason_code="WORK_SUBMITTED",
                reason=(
                    f"{artifact.ref} submitted with {len(artifact.findings)} finding(s) "
                    f"backed by {len(artifact.evidence)} evidence item(s)"
                ),
                correlation_id=task.id,
            ).entity
        )

        # Free the agent now: its work is submitted, and holding the slot until the
        # review finishes would make reviewer throughput limit producer throughput.
        self._delegation.release(agent.id, task)
        return node, assignment, artifact

    # ---- artifact construction ------------------------------------------

    def _build_artifact(
        self,
        task: Task,
        node: TaskNode,
        assignment: TaskAssignment,
        result: WorkResult,
        actor: ActorRef,
    ) -> Artifact:
        locator_to_id: dict[str, UUID] = {e.locator: e.id for e in result.evidence}

        findings: list[Finding] = []
        for draft in result.findings:
            unknown = [loc for loc in draft.evidence_locators if loc not in locator_to_id]
            if unknown:
                raise InvariantViolation(
                    "I9",
                    f"{node.ref}: finding '{draft.statement[:60]}' cites evidence locators "
                    f"that were not attached: {unknown}",
                )
            findings.append(
                Finding(
                    statement=draft.statement,
                    evidence_ids=tuple(locator_to_id[loc] for loc in draft.evidence_locators),
                    confidence=draft.confidence,
                )
            )

        previous = self._latest_artifact(node)
        now = self._rt.clock.now()
        artid, ref = self._rt.mint("ART")
        artifact = Artifact(
            id=artid,
            ref=ref,
            status="DRAFT",
            created_at=now,
            created_by=actor,
            updated_at=now,
            task_id=task.id,
            node_id=node.id,
            assignment_id=assignment.id,
            artifact_type=node.expected_artifact_type,
            title=result.title,
            content=result.content,
            findings=tuple(findings),
            evidence=result.evidence,
            confidence=result.confidence,
            uncertainty=result.uncertainty,
            methodology=result.methodology,
            data_sources=result.data_sources,
            derived_from_artifact_ids=node.input_artifact_ids,
            supersedes_artifact_id=previous.id if previous else None,
            artifact_version=(previous.artifact_version + 1) if previous else 1,
        )
        artifact.content_hash = artifact.compute_hash()
        artifact = self._rt.repos.artifacts.insert(artifact)

        self._engine.record_creation(
            artifact,
            actor=actor,
            reason_code="ARTIFACT_SUBMITTED",
            reason=(f"produced for {node.ref} attempt {node.attempt}: {result.title}"),
            correlation_id=task.id,
            event_name="qor.artifact.created",
            payload={
                "node": node.ref,
                "type": artifact.artifact_type.value,
                "findings": len(artifact.findings),
                "evidence": len(artifact.evidence),
                "version": artifact.artifact_version,
            },
        )

        if previous is not None:
            self._rt.repos.artifacts.save(
                self._engine.fire(
                    previous,
                    "supersede",
                    actor=actor,
                    reason_code="ARTIFACT_SUPERSEDED",
                    reason=f"replaced by {artifact.ref} on rework attempt {node.attempt}",
                    correlation_id=task.id,
                ).entity
            )
        return artifact

    def _submit_artifact(self, task: Task, artifact: Artifact, actor: ActorRef) -> Artifact:
        return self._rt.repos.artifacts.save(
            self._engine.fire(
                artifact,
                "submit",
                actor=actor,
                reason_code="ARTIFACT_SUBMITTED",
                reason=(f"{len(artifact.findings)} finding(s) all traceable to attached evidence"),
                correlation_id=task.id,
            ).entity
        )

    # ---- helpers -------------------------------------------------------

    def _input_artifacts(self, task: Task, node: TaskNode) -> tuple[Artifact, ...]:
        """Accepted upstream artifacts, so a synthesis node integrates real output."""
        if task.active_plan_id is None:
            return ()
        plan = self._rt.repos.plans.get(task.active_plan_id)
        upstream = plan.upstream_of(node.id)
        collected: list[Artifact] = []
        for nid in upstream:
            dep = self._rt.repos.nodes.get(nid)
            for aid in dep.output_artifact_ids:
                artifact = self._rt.repos.artifacts.get(aid)
                if artifact.status == "ACCEPTED":
                    collected.append(artifact)
        if upstream and not collected and node.node_kind == NodeKind.WORK:
            raise QORError(f"{node.ref} cannot execute without an ACCEPTED upstream artifact")
        if node.node_kind == NodeKind.SYNTHESIS:
            missing = [
                self._rt.repos.nodes.get(nid).ref
                for nid in upstream
                if self._rt.repos.nodes.get(nid).status == "ACCEPTED"
                and not any(
                    self._rt.repos.artifacts.get(aid).status == "ACCEPTED"
                    for aid in self._rt.repos.nodes.get(nid).output_artifact_ids
                )
            ]
            if missing:
                raise QORError(
                    f"{node.ref} cannot synthesise without ACCEPTED artifacts from {missing}"
                )
        # Handoff copies so a downstream agent cannot mutate the organization's artifact.
        return tuple(art.model_copy(deep=True) for art in collected)

    def _required_changes(self, node: TaskNode) -> tuple[str, ...]:
        """What the previous round's reviewer asked for, if this is a rework."""
        if node.attempt <= 1:
            return ()
        prior = [
            r
            for r in self._rt.repos.reviews.all()
            if r.node_id == node.id and r.round == node.attempt - 1
        ]
        changes: list[str] = []
        for review in prior:
            changes += list(review.required_changes)
        return tuple(dict.fromkeys(changes))

    def _latest_artifact(self, node: TaskNode) -> Artifact | None:
        candidates = [
            a
            for a in self._rt.repos.artifacts.all()
            if a.node_id == node.id and a.status != "SUPERSEDED"
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda a: a.artifact_version)[-1]
