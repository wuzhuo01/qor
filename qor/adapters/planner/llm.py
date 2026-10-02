"""LLMPlanner -- an AI CIO that genuinely decides how to decompose the objective.

The prompt hands the model a capability catalog and asks for a DAG. It never
mentions agents, so the model cannot produce an "Agent A then Agent B" flow even if
it wanted to; the vocabulary is not available to it.

Whatever comes back is untrusted. It is parsed, validated into a ``PlanDraft``, and
then validated again by the planning service against the catalog and policy. A model
that hallucinates a capability produces a rejected plan, not a corrupted run.
"""

from __future__ import annotations

import json
import re
from typing import Any

from qor.domain.enums import ArtifactType
from qor.domain.task import NodeKind
from qor.kernel.errors import InvariantViolation, QORError
from qor.ports.llm import LLMPort, LLMUnavailable
from qor.ports.planner import NodeDraft, PlanDraft, PlanRequest, binding_constraint_violations

__all__ = ["LLMPlanner", "PlannerOutputInvalid"]


class PlannerOutputInvalid(QORError):
    """The model returned something that is not a usable plan."""


SYSTEM_PROMPT = """You plan work for an AI-native organization.

You are given an objective and a catalog of capabilities the organization can perform.
Decompose the objective into a task graph.

Hard rules:
1. Address work ONLY by capability key from the catalog. Never invent a capability.
2. Never name a person, agent, team or role. You are planning, not assigning.
3. The graph must be acyclic. `depends_on` may only reference keys you define.
4. Independent lines of evidence must be separate nodes and must NOT depend on each
   other. If two analyses can be done independently, they must be.
5. Exactly one node must produce SYNTHESIS and exactly one must produce
   RECOMMENDATION. Both must have node_kind SYNTHESIS and must depend on upstream work.
6. Explain your decomposition in decision_rationale. State why you split it this way.
   Quote distinctive terms from the OBJECTIVE so the rationale is about this
   objective, not a reusable template.
7. If BINDING CONSTRAINTS are present, the plan is illegal unless EACH is satisfied:
   For ADDRESS_CONTRADICTED_OUTCOME a WORK node MUST set addresses_outcome_ref to that
   constraint's outcome_ref, and the RECOMMENDATION node MUST depend on that node
   (directly or via synthesis). For ADDRESS_FULFILLED_RESPONSE a WORK node MUST set
   addresses_decision_ref to that constraint's decision_ref. The same WORK node may
   carry both fields. Quoting refs in decision_rationale is not a substitute.

Return ONLY a JSON object. No prose, no markdown fences."""

SCHEMA_HINT = """{
  "decision_rationale": "string, why this decomposition",
  "nodes": [
    {
      "key": "n1",
      "name": "short label",
      "objective": "what must be established",
      "required_capabilities": ["capability.key"],
      "expected_artifact_type": "ANALYSIS_REPORT|ANALYSIS_RESULT|RISK_ASSESSMENT|VALIDATION_REPORT|SYNTHESIS|RECOMMENDATION",
      "node_kind": "WORK|SYNTHESIS",
      "depends_on": ["n1"],
      "review_required": true,
      "addresses_outcome_ref": "",
      "addresses_decision_ref": ""
    }
  ]
}"""


class LLMPlanner:
    kind = "llm"

    def __init__(self, llm: LLMPort, *, max_repair_attempts: int = 1) -> None:
        self._llm = llm
        self._max_repair = max_repair_attempts
        self.plan_invocations = 0

    @property
    def provider(self) -> str:
        return self._llm.name

    def plan(self, request: PlanRequest) -> PlanDraft:
        self.plan_invocations += 1
        user = self._render_prompt(request)
        last_error = ""
        for attempt in range(self._max_repair + 1):
            prompt = (
                user
                if attempt == 0
                else f"{user}\n\nYour previous reply was rejected: {last_error}\nReturn corrected JSON only."
            )
            try:
                raw = self._llm.complete_json(
                    system=SYSTEM_PROMPT,
                    user=prompt,
                    schema_hint=SCHEMA_HINT,
                    # Reasoning models spend completion tokens on chain-of-thought.
                    # 2048 was fully consumed by reasoning, leaving empty/truncated JSON.
                    max_tokens=8192,
                    task_ref=request.task_ref,
                )
            except LLMUnavailable as exc:
                # Empty / truncated / timed-out replies are unusable transport output,
                # not a legal plan. Retry them inside the existing bound. Configuration
                # and unexpected-shape failures stay fail-closed without another call.
                if not _retryable_llm_unavailable(exc) or attempt >= self._max_repair:
                    raise
                last_error = str(exc)
                continue
            try:
                return self._parse(raw, request)
            except (PlannerOutputInvalid, InvariantViolation, ValueError) as exc:
                # InvariantViolation is included deliberately: a structural mistake
                # such as a dependency cycle is the kind of error a model makes, and
                # it deserves the same repair attempt as malformed JSON.
                last_error = str(exc)
        raise PlannerOutputInvalid(
            f"{self._llm.name} did not produce a valid plan after "
            f"{self._max_repair + 1} attempt(s): {last_error}"
        )

    # ---- prompt --------------------------------------------------------

    def _render_prompt(self, request: PlanRequest) -> str:
        catalog = "\n".join(
            f"- {offer.key} [{offer.risk_class}]: {offer.description}"
            for offer in request.available_capabilities
        )
        outputs = (
            ", ".join(t.value for t in request.required_outputs) or "SYNTHESIS, RECOMMENDATION"
        )
        lines = [
            f"OBJECTIVE: {request.objective}",
            f"TASK RISK LEVEL: {request.risk_level.name}",
            f"REQUIRED OUTPUT ARTIFACT TYPES: {outputs}",
            f"MAXIMUM NODES: {request.max_nodes}",
            "",
            "AVAILABLE CAPABILITIES:",
            catalog,
        ]
        if request.previous_failure_feedback:
            lines += [
                "",
                "A PREVIOUS PLAN FAILED. Address these problems:",
                *(f"- {item}" for item in request.previous_failure_feedback),
            ]
        if request.prior_outcomes:
            lines += ["", "ORGANIZATION FACTS (prior outcomes):"]
            for fact in request.prior_outcomes:
                lines.append(
                    f"- {fact.ref} decision={fact.decision_ref} subject={fact.subject_key} "
                    f"verdict={fact.verdict} confidence={fact.confidence} status={fact.status}"
                    + (
                        f" fulfilling_decision={fact.fulfilling_decision_ref}"
                        if fact.fulfilling_decision_ref
                        else ""
                    )
                )
                if fact.statement:
                    lines.append(f"  {fact.statement}")
        if request.binding_constraints:
            lines += [
                "",
                "BINDING CONSTRAINTS (the plan is rejected unless each is satisfied):",
            ]
            for constraint in request.binding_constraints:
                lines.append(
                    f"- {constraint.kind} outcome_ref={constraint.outcome_ref} "
                    f"subject={constraint.subject_key} "
                    f"contradicted_decision_type={constraint.contradicted_decision_type}"
                    + (
                        f" decision_ref={constraint.decision_ref}"
                        if constraint.decision_ref
                        else ""
                    )
                )
            lines.append(
                "For ADDRESS_CONTRADICTED_OUTCOME, include a WORK node whose "
                "addresses_outcome_ref equals that outcome_ref, and make RECOMMENDATION "
                "depend on it. For ADDRESS_FULFILLED_RESPONSE, include a WORK node whose "
                "addresses_decision_ref equals that decision_ref."
            )
        return "\n".join(lines)

    # ---- parsing -------------------------------------------------------

    def _parse(self, raw: str, request: PlanRequest) -> PlanDraft:
        data = self._load_json(raw)
        if not isinstance(data, dict):
            raise PlannerOutputInvalid("top-level JSON value is not an object")

        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise PlannerOutputInvalid("'nodes' must be a non-empty array")

        offered = {offer.key for offer in request.available_capabilities}
        nodes: list[NodeDraft] = []
        for index, item in enumerate(raw_nodes):
            if not isinstance(item, dict):
                raise PlannerOutputInvalid(f"node at index {index} is not an object")
            capabilities = tuple(str(c) for c in item.get("required_capabilities", ()))
            unknown = [c for c in capabilities if c not in offered]
            if unknown:
                raise PlannerOutputInvalid(
                    f"node '{item.get('key', index)}' requires capabilities not in the "
                    f"catalog: {unknown}"
                )
            nodes.append(
                NodeDraft(
                    key=str(item.get("key") or f"n{index + 1}"),
                    name=str(item.get("name", "")).strip() or f"node {index + 1}",
                    objective=str(item.get("objective", "")).strip()
                    or f"advance: {request.objective}",
                    required_capabilities=capabilities,
                    expected_artifact_type=self._artifact_type(item.get("expected_artifact_type")),
                    node_kind=self._node_kind(item.get("node_kind")),
                    depends_on=tuple(str(d) for d in item.get("depends_on", ())),
                    review_required=bool(item.get("review_required", True)),
                    addresses_outcome_ref=str(item.get("addresses_outcome_ref") or "").strip(),
                    addresses_decision_ref=str(item.get("addresses_decision_ref") or "").strip(),
                )
            )

        rationale = str(data.get("decision_rationale", "")).strip()
        if not rationale:
            raise PlannerOutputInvalid("'decision_rationale' is required and was empty")

        violations = binding_constraint_violations(tuple(nodes), request.binding_constraints)
        if violations:
            raise PlannerOutputInvalid("; ".join(violations))

        return PlanDraft(
            nodes=tuple(nodes),
            decision_rationale=rationale,
            expected_outputs=tuple(dict.fromkeys(n.expected_artifact_type for n in nodes)),
        )

    @staticmethod
    def _load_json(raw: str) -> Any:
        text = raw.strip()
        if not text:
            raise PlannerOutputInvalid(
                "reply is empty; unusable LLM output, not a legal planner document"
            )
        # Models often wrap JSON in fences despite instructions.
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        if not text:
            raise PlannerOutputInvalid(
                "reply is empty; unusable LLM output, not a legal planner document"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlannerOutputInvalid(f"reply is not valid JSON: {exc}") from exc

    @staticmethod
    def _artifact_type(value: object) -> ArtifactType:
        try:
            return ArtifactType(str(value))
        except ValueError:
            raise PlannerOutputInvalid(
                f"expected_artifact_type '{value}' is not one of {[t.value for t in ArtifactType]}"
            ) from None

    @staticmethod
    def _node_kind(value: object) -> str:
        kind = str(value or NodeKind.WORK).upper()
        if kind not in NodeKind.ALL:
            raise PlannerOutputInvalid(f"node_kind '{value}' is not one of {NodeKind.ALL}")
        return kind


def _retryable_llm_unavailable(exc: LLMUnavailable) -> bool:
    """Empty, length-truncated, or timed-out replies may be retried once.

    Missing configuration, unexpected payload shape, and exhausted scripted
    doubles are not transient output failures and must fail closed immediately.
    """
    message = str(exc).lower()
    if "no llm provider configured" in message:
        return False
    if "unexpected payload shape" in message:
        return False
    if "no scripted replies left" in message:
        return False
    return "empty content" in message or "finish_reason=length" in message or "timed out" in message
