"""Artifact -- the only form work output may take (invariant I4).

An agent cannot "just answer". It produces an Artifact, and an Artifact whose
findings do not point at evidence is rejected at submission time rather than at
review time. That is Evidence First expressed as a type constraint.

Content is immutable once written (I-AR1). Corrections produce a new version and
supersede the old one, so "what did we believe last Tuesday" stays answerable.
"""

from __future__ import annotations

import hashlib
import json
from typing import ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qor.domain.base import Entity
from qor.domain.enums import ArtifactType
from qor.domain.values import Confidence, EvidenceItem, Uncertainty
from qor.kernel.errors import InvariantViolation

__all__ = ["Artifact", "Finding"]


class Finding(BaseModel):
    """One claim, plus the evidence ids that support it.

    ``evidence_ids`` is required and validated against the artifact's own evidence
    list. A finding with no traceable support is exactly the thing reviews are
    supposed to catch, so it is cheaper to make it unrepresentable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str
    evidence_ids: tuple[UUID, ...]
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def _has_support(self) -> Self:
        if not self.statement.strip():
            raise InvariantViolation("I9", "Finding.statement must be non-empty")
        if not self.evidence_ids:
            raise InvariantViolation("I9", f"Finding '{self.statement[:60]}' cites no evidence")
        return self


class Artifact(Entity):
    ENTITY_TYPE: ClassVar[str] = "Artifact"
    REF_PREFIX: ClassVar[str] = "ART"

    task_id: UUID
    node_id: UUID | None = None
    assignment_id: UUID | None = None
    artifact_type: ArtifactType
    title: str

    # V0.1 allows content to be text (requirement §8). Structure lives in findings.
    content: str = ""
    findings: tuple[Finding, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()

    confidence: Confidence | None = None
    uncertainty: Uncertainty = Field(default_factory=Uncertainty)

    # Permitted to be empty in V0.1 (Human CIO ruling, correction 3).
    data_sources: tuple[str, ...] = ()
    methodology: str = ""
    reproducibility: dict[str, str] = Field(default_factory=dict)

    derived_from_artifact_ids: tuple[UUID, ...] = ()
    supersedes_artifact_id: UUID | None = None
    artifact_version: int = 1
    content_hash: str = ""

    @model_validator(mode="after")
    def _findings_trace_to_evidence(self) -> Self:
        """Invariant I9: every finding's evidence must actually be present."""
        known = {e.id for e in self.evidence}
        for finding in self.findings:
            missing = [str(eid) for eid in finding.evidence_ids if eid not in known]
            if missing:
                raise InvariantViolation(
                    "I9",
                    f"finding '{finding.statement[:60]}' cites evidence not in this "
                    f"artifact: {missing}",
                )
        return self

    def compute_hash(self) -> str:
        """Stable digest over the substantive content, for tamper detection."""
        payload = json.dumps(
            {
                "content": self.content,
                "findings": [
                    {
                        "statement": f.statement,
                        "evidence_ids": sorted(str(i) for i in f.evidence_ids),
                    }
                    for f in self.findings
                ],
                "evidence": sorted(f"{e.kind}|{e.locator}|{e.summary}" for e in self.evidence),
                "methodology": self.methodology,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def has_traceable_findings(self) -> bool:
        return bool(self.findings) and bool(self.evidence)
