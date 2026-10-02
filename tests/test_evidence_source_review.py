"""Review rejects a fact that cites reasoning, and does not inspect industry wording."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from qor.domain.artifact import Artifact, Finding
from qor.domain.enums import ArtifactType, ClaimClass, EvidenceSource, FindingSeverity
from qor.domain.values import EvidenceItem
from qor.kernel.actor import ActorRef
from qor.ports.agent_runtime import ReviewOpinion
from qor.services.evidence_source import apply_evidence_source_rule


def _artifact(claim: ClaimClass, kind: EvidenceSource, statement: str) -> Artifact:
    evidence = EvidenceItem(kind=kind, summary="support", locator="loc://1", produced_by="AGT-1")
    now = datetime(2026, 10, 2, tzinfo=UTC)
    return Artifact(
        id=uuid4(),
        ref="ART-TEST-001",
        status="SUBMITTED",
        created_at=now,
        created_by=ActorRef.system(),
        updated_at=now,
        task_id=uuid4(),
        artifact_type=ArtifactType.ANALYSIS_REPORT,
        title="note",
        content=statement,
        findings=(Finding(statement=statement, evidence_ids=(evidence.id,), claim_class=claim),),
        evidence=(evidence,),
    )


def _passed() -> ReviewOpinion:
    return ReviewOpinion(verdict="PASSED", summary="reviewer accepted the wording")


def test_reasoning_supports_a_hypothesis() -> None:
    artifact = _artifact(
        ClaimClass.HYPOTHESIS,
        EvidenceSource.REASONING,
        "The series shows a return anomaly.",
    )
    opinion = apply_evidence_source_rule(artifact, _passed())
    assert opinion.verdict == "PASSED"
    assert opinion.findings == ()


def test_reasoning_cannot_support_a_market_fact() -> None:
    artifact = _artifact(
        ClaimClass.FACT,
        EvidenceSource.REASONING,
        "The series shows a return anomaly.",
    )
    opinion = apply_evidence_source_rule(artifact, _passed())
    assert opinion.verdict == "FAILED"
    assert opinion.findings[0].severity is FindingSeverity.BLOCKER
    assert "reasoning" in opinion.findings[0].statement
    assert opinion.required_changes


def test_external_evidence_supports_a_fact() -> None:
    artifact = _artifact(
        ClaimClass.FACT,
        EvidenceSource.EXTERNAL,
        "The series shows a return anomaly.",
    )
    opinion = apply_evidence_source_rule(artifact, _passed())
    assert opinion.verdict == "PASSED"
    assert opinion.findings == ()
