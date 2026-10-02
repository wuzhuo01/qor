"""The public organization-runtime example stays outside qor/."""

from __future__ import annotations

from examples.organization_runtime.scenario import OBJECTIVE, render, run_full, run_gap


def test_full_example_reaches_outcome_and_separates_evidence_sources():
    run = run_full()
    app = run.app
    task = app.runtime.repos.tasks.get(run.result.task.id)

    assert task.objective == OBJECTIVE
    assert task.status == "COMPLETED"
    assert run.result.plan is not None
    assert app.runtime.repos.assignments.all()
    assert run.app.planner.kind == "stub"
    assert app.runtime.llm_calls == []

    artifacts = [item for item in app.runtime.repos.artifacts.all() if item.task_id == task.id]
    kinds = {item.kind for artifact in artifacts for item in artifact.evidence}
    assert kinds == {"external", "tool", "reasoning"}

    reviews = [item for item in app.runtime.repos.reviews.all() if item.task_id == task.id]
    failed = [item for item in reviews if item.verdict == "FAILED"]
    passed = [item for item in reviews if item.verdict == "PASSED"]
    assert failed and passed

    failed_artifact = app.runtime.repos.artifacts.get(failed[0].artifact_id)
    assert any(item.kind == "reasoning" for item in failed_artifact.evidence)
    assert any(
        finding.claim_class is not None and finding.claim_class.value == "fact"
        for finding in failed_artifact.findings
    )

    passed_kinds = set()
    for review in passed:
        artifact = app.runtime.repos.artifacts.get(review.artifact_id)
        passed_kinds.update(item.kind for item in artifact.evidence)
    assert "external" in passed_kinds
    assert "tool" in passed_kinds

    decision = run.result.decision
    assert decision is not None
    assert decision.status == "DECIDED"
    assert decision.based_on_artifact_ids
    assert decision.rationale.startswith("Decision basis:")
    assert run.outcome is not None
    assert run.outcome.decision_id == decision.id
    assert app.runtime.log.verify_audit_chain()

    text = render(run)
    for heading in (
        "organization",
        "task timeline",
        "artifacts",
        "evidence sources",
        "review result",
        "decision",
        "audit verification",
    ):
        assert heading in text


def test_gap_example_stops_on_capability_gap():
    run = run_gap()
    task = run.app.runtime.repos.tasks.get(run.result.task.id)
    assert task.status == "BLOCKED"
    assert run.result.decision is None
    assert run.outcome is None
    codes = [
        step.reason_code
        for step in run.app.runtime.log.transitions_for(task.id)
        if step.transition == "block"
    ]
    assert codes == ["CAPABILITY_GAP"]
    assert "CAPABILITY_GAP" in render(run)
    assert run.app.runtime.log.verify_audit_chain()
