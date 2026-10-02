"""StubPlanner -- deterministic planner for CI.

**Test-only. This does not represent QOR's real planning capability** (Human CIO
ruling E4).

It is deterministic but not hardcoded: the plan is *derived* from the capability
catalog it is handed. Add a capability and the plan grows a node until the request's
node limit; remove one and it shrinks. Integration nodes use a capability from that
catalog. There is no literal task shape and no agent name anywhere in this file.

That distinction matters. A stub that returned a fixed six-node plan with fixed
names would make T2 and T13 meaningless -- it would be the "fixed workflow
disguised as planning" that V0.1 exists to rule out.
"""

from __future__ import annotations

from qor.domain.enums import ArtifactType
from qor.domain.task import NodeKind
from qor.ports.planner import NodeDraft, PlanDraft, PlanRequest

__all__ = ["StubPlanner"]

# Capabilities that are organizational machinery, not plannable work.
EXCLUDED_NAMESPACES = ("governance",)


class StubPlanner:
    kind = "stub"

    @property
    def provider(self) -> str:
        return "stub"

    def plan(self, request: PlanRequest) -> PlanDraft:
        work = [
            offer.key
            for offer in request.available_capabilities
            if not offer.key.startswith(EXCLUDED_NAMESPACES)
        ]
        if not work:
            raise ValueError("no plannable capabilities were offered")
        # The last offered capability integrates the others. Nothing here looks
        # at a capability's name, so a catalog without "*.synthesis" still plans.
        integrating = work[-1]
        evidence_room = max(request.max_nodes - 2 - len(request.binding_constraints), 1)
        evidence_pool = work[:-1]
        evidence_caps = evidence_pool[:evidence_room] if evidence_pool else [integrating]

        nodes: list[NodeDraft] = []
        for index, key in enumerate(evidence_caps, start=1):
            nodes.append(
                NodeDraft(
                    key=f"n{index}",
                    name=self._name_for(key),
                    objective=(f"Apply {key} to the objective: {request.objective}"),
                    required_capabilities=(key,),
                    expected_artifact_type=self._artifact_for(key),
                    node_kind=NodeKind.WORK,
                )
            )

        evidence_keys = tuple(n.key for n in nodes)
        for index, constraint in enumerate(request.binding_constraints, start=1):
            cap = evidence_caps[0]
            key = f"address_{index}"
            nodes.append(
                NodeDraft(
                    key=key,
                    name="Re-examine contradicted outcome",
                    objective=(
                        f"Re-examine {constraint.contradicted_decision_type} on "
                        f"{constraint.subject_key} given contradicted outcome "
                        f"{constraint.outcome_ref}"
                    ),
                    required_capabilities=(cap,),
                    expected_artifact_type=self._artifact_for(cap),
                    node_kind=NodeKind.WORK,
                    addresses_outcome_ref=constraint.outcome_ref,
                    addresses_decision_ref=constraint.decision_ref,
                )
            )
            evidence_keys = (*evidence_keys, key)

        synthesis_node = f"n{len(nodes) + 1}"
        nodes.append(
            NodeDraft(
                key=synthesis_node,
                name="Integrate findings",
                objective="Reconcile the evidence nodes into one coherent account.",
                required_capabilities=(integrating,),
                expected_artifact_type=ArtifactType.SYNTHESIS,
                node_kind=NodeKind.SYNTHESIS,
                depends_on=evidence_keys,
            )
        )
        nodes.append(
            NodeDraft(
                key=f"n{len(nodes) + 1}",
                name="Form recommendation",
                objective="Turn the integrated account into an actionable recommendation.",
                required_capabilities=(integrating,),
                expected_artifact_type=ArtifactType.RECOMMENDATION,
                node_kind=NodeKind.SYNTHESIS,
                depends_on=(synthesis_node,),
            )
        )

        rationale = (
            f"Derived {len(evidence_caps)} evidence nodes from the offered capability "
            f"catalog ({', '.join(evidence_caps)}). Integration and recommendation use "
            f"{integrating}, one of the offered capabilities. "
            f"Evidence nodes are kept independent so that no single analysis can quietly "
            f"determine the conclusion."
        )
        if request.previous_failure_feedback:
            rationale += " Replanning after: " + "; ".join(request.previous_failure_feedback)

        return PlanDraft(
            nodes=tuple(nodes),
            strategy_rationale=rationale,
            expected_outputs=tuple(dict.fromkeys(n.expected_artifact_type for n in nodes)),
        )

    @staticmethod
    def _name_for(capability_key: str) -> str:
        namespace, _, verb = capability_key.partition(".")
        return f"{namespace.title()}: {verb.replace('_', ' ')}"

    @staticmethod
    def _artifact_for(_capability_key: str) -> ArtifactType:
        return ArtifactType.ANALYSIS_REPORT
