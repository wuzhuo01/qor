"""Reject a factual claim that is supported only by reasoning.

The check looks at the finding's claim class and the cited evidence kind.
It does not read the wording of the claim.
"""

from __future__ import annotations

from qor.domain.artifact import Artifact
from qor.domain.enums import ClaimClass, EvidenceSource, FindingSeverity
from qor.ports.agent_runtime import OpinionFinding, ReviewOpinion

_REASONING_MAY_SUPPORT = frozenset(
    {ClaimClass.HYPOTHESIS, ClaimClass.SPECULATION, ClaimClass.DIRECTION}
)


def reasoning_fact_conflicts(artifact: Artifact) -> tuple[str, ...]:
    known = {item.id: item for item in artifact.evidence}
    problems: list[str] = []
    for finding in artifact.findings:
        if finding.claim_class is None or finding.claim_class in _REASONING_MAY_SUPPORT:
            continue
        for evidence_id in finding.evidence_ids:
            evidence = known[evidence_id]
            if evidence.kind != EvidenceSource.REASONING:
                continue
            problems.append(
                "factual claim cites reasoning evidence; "
                "reasoning can support a hypothesis, speculation, or research direction"
            )
    return tuple(problems)


def apply_evidence_source_rule(artifact: Artifact, opinion: ReviewOpinion) -> ReviewOpinion:
    problems = reasoning_fact_conflicts(artifact)
    if not problems:
        return opinion
    findings = [
        *opinion.findings,
        *[
            OpinionFinding(
                severity=FindingSeverity.BLOCKER,
                statement=problem,
                location="evidence.kind",
            )
            for problem in problems
        ],
    ]
    return opinion.model_copy(
        update={
            "verdict": "FAILED",
            "findings": tuple(findings),
            "summary": problems[0],
            "required_changes": (
                *opinion.required_changes,
                "cite external or tool evidence for a factual claim, "
                "or classify it as a hypothesis, speculation, or research direction",
            ),
        }
    )
