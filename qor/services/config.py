"""Configuration loading and policy evaluation.

Policies answer "must this be reviewed / approved / gated", and they answer it from
YAML rather than from branches scattered through services. That is what makes it
possible to change how cautious the organization is without editing its logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from qor.domain.enums import Environment, RiskClass, RiskLevel

__all__ = ["ApprovalOutcome", "Policies", "QORConfig", "ReviewRequirement"]

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"


@dataclass(frozen=True)
class ReviewRequirement:
    required: bool
    review_types: tuple[str, ...]
    min_reviewers: int
    reviewer_capability: str
    policy_ref: str
    basis: str


@dataclass(frozen=True)
class ApprovalOutcome:
    """Whether a human must approve, and why or why not.

    ``WAIVED`` is represented explicitly rather than as ``required=False`` so the
    Action Gateway can record that the check ran and what waived it. Skipping a
    check and passing a check are different facts.
    """

    required: bool
    verdict: str  # PASSED | WAIVED | FAILED
    policy_ref: str
    basis: str


class Policies:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    # ---- review --------------------------------------------------------

    def review_requirement(
        self,
        *,
        task_risk: RiskLevel,
        capabilities: tuple[str, ...],
        capability_review_types: tuple[str, ...],
    ) -> ReviewRequirement:
        cfg = self._raw["review"]
        threshold = RiskLevel.parse(cfg["always_required_at_or_above"])
        always = set(cfg.get("always_required_capabilities", ()))

        reasons: list[str] = []
        if task_risk >= threshold:
            reasons.append(f"task risk {task_risk} >= {threshold}")
        hit = sorted(set(capabilities) & always)
        if hit:
            reasons.append(f"capability always requires review: {hit}")
        if capability_review_types:
            reasons.append(
                f"capability declares mandatory review types {list(capability_review_types)}"
            )

        types = tuple(capability_review_types) or ("METHODOLOGY",)
        return ReviewRequirement(
            required=bool(reasons),
            review_types=types,
            min_reviewers=int(cfg.get("min_reviewers", 1)),
            reviewer_capability=str(cfg.get("reviewer_capability", "governance.review")),
            policy_ref="POLICY-REVIEW-V1",
            basis="; ".join(reasons) if reasons else "no review trigger matched",
        )

    def review_rubric(self) -> tuple[str, ...]:
        return tuple(self._raw["review"].get("rubric", ()))

    # ---- approval ------------------------------------------------------

    def approval_outcome(
        self,
        *,
        environment: Environment,
        task_risk: RiskLevel,
        irreversible: bool,
        capability_risk_class: RiskClass,
    ) -> ApprovalOutcome:
        cfg = self._raw["approval"]
        rules = cfg["required_when"]
        triggers: list[str] = []

        if environment.value in rules.get("environment_in", ()):
            triggers.append(f"environment={environment.value}")
        risk_threshold = RiskLevel.parse(rules["task_risk_at_or_above"])
        if task_risk >= risk_threshold:
            triggers.append(f"task risk {task_risk} >= {risk_threshold}")
        if irreversible and rules.get("decision_irreversible", False):
            triggers.append("decision is irreversible")
        class_threshold = RiskClass.parse(rules["capability_risk_class_at_or_above"])
        if capability_risk_class >= class_threshold:
            triggers.append(f"capability risk class {capability_risk_class} >= {class_threshold}")

        if triggers:
            return ApprovalOutcome(
                required=True,
                verdict="PASSED",
                policy_ref="POLICY-APPROVAL-V1",
                basis="approval required: " + "; ".join(triggers),
            )
        return ApprovalOutcome(
            required=False,
            verdict="WAIVED",
            policy_ref=str(cfg["waiver_policy_ref"]),
            basis=(
                f"no approval trigger matched (environment={environment.value}, "
                f"task_risk={task_risk}, capability_risk_class={capability_risk_class})"
            ),
        )

    def effective_risk_class(
        self, *, nominal: RiskClass, dry_run: bool, environment: Environment
    ) -> tuple[RiskClass, str]:
        """Reduce a capability's nominal risk class to what it can actually do here.

        A capability may be REALITY_AFFECTING, but a dry run in a sandbox cannot
        affect reality no matter what the capability is nominally able to do.
        The reduction is returned together with its reason so
        callers record *why* the class was lowered instead of silently lowering it.

        This lives in one place because two callers need it -- the decision service
        (to decide whether approval is required) and the action gateway (to run the
        approval check). Two copies would eventually disagree, and did.
        """
        if dry_run and environment is Environment.SANDBOX:
            reduced = min(nominal, RiskClass.SIMULATION_AFFECTING)
            if reduced != nominal:
                return reduced, (
                    f"nominal capability class {nominal} reduced to {reduced} because "
                    f"dry_run=true in {environment.value}"
                )
            return reduced, f"capability class {nominal} unchanged"
        return nominal, f"capability class {nominal} applies in full ({environment.value})"

    # ---- action --------------------------------------------------------

    def dry_run_stop_state(self) -> str:
        return str(self._raw["action"]["dry_run_stop_state"])

    def allowed_environments(self, task_risk: RiskLevel) -> tuple[str, ...]:
        return tuple(self._raw["action"]["allowed_environments"][task_risk.name])

    def requires_dry_run_when_irreversible(self) -> bool:
        return bool(self._raw["action"]["require_dry_run_when_irreversible"])

    # ---- delegation / planning / execution -----------------------------

    def delegation_weights(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._raw["delegation"]["weights"].items()}

    def delegation_filters(self) -> tuple[str, ...]:
        return tuple(self._raw["delegation"]["filters"])

    def max_nodes(self) -> int:
        return int(self._raw["planning"]["max_nodes"])

    def max_plan_revisions(self) -> int:
        return int(self._raw["planning"]["max_plan_revisions"])

    def enforce_output_coverage(self) -> bool:
        return bool(self._raw["planning"]["enforce_output_coverage"])

    def max_attempts_per_node(self) -> int:
        return int(self._raw["execution"]["max_attempts_per_node"])

    def lease_seconds(self) -> int:
        return int(self._raw["execution"]["lease_seconds"])


@dataclass(frozen=True)
class QORConfig:
    organization: dict[str, Any]
    capabilities: dict[str, Any]
    roles: dict[str, Any]
    permissions: dict[str, Any]
    policies: Policies
    state_machine_dir: Path

    @classmethod
    def load(cls, root: Path | None = None, *, profile: str | None = None) -> QORConfig:
        """Load configuration.

        The default load is the generic organization. ``profile`` names an overlay
        directory under ``profiles/``. ``generic`` matches the default organization
        and capability catalog. Other files stay shared unless that overlay
        replaces them.
        """
        base = root or CONFIG_ROOT
        overlay = base / "profiles" / profile if profile else None

        def read(name: str) -> dict[str, Any]:
            path = base / name
            if overlay is not None and (overlay / name).is_file():
                path = overlay / name
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        return cls(
            organization=read("organization.yaml"),
            capabilities=read("capabilities.yaml"),
            roles=read("roles.yaml"),
            permissions=read("permissions.yaml"),
            policies=Policies(read("policies.yaml")),
            state_machine_dir=base / "state_machines",
        )

    # ---- permission catalog queries -------------------------------------

    def plannable_capability_keys(self) -> tuple[str, ...]:
        """Keys a planner may be offered as work.

        Same exclusion the planning service uses: reality-affecting capabilities
        and capabilities that carry required permissions are organizational
        machinery, not plannable work.
        """
        keys: list[str] = []
        for spec in self.capabilities.get("capabilities", ()):
            if spec.get("risk_class") == "REALITY_AFFECTING":
                continue
            if spec.get("required_permissions"):
                continue
            keys.append(str(spec["key"]))
        return tuple(keys)

    @property
    def known_permissions(self) -> frozenset[str]:
        return frozenset(self.permissions.get("permissions", ()))

    @property
    def human_cio_exclusive(self) -> frozenset[str]:
        return frozenset(self.permissions.get("human_cio_exclusive", ()))

    @property
    def system_only_fields(self) -> dict[str, tuple[str, ...]]:
        return {
            entity: tuple(fields)
            for entity, fields in (self.permissions.get("system_only_fields") or {}).items()
        }
