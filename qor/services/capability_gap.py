"""Read-only comparison of a plan's required capabilities and agent holdings.

This does not create tasks, change state, or choose who should receive a capability.
"""

from __future__ import annotations

from dataclasses import dataclass

from qor.domain.task import TaskPlan
from qor.services.runtime import Runtime

__all__ = ["CapabilityGapLine", "CapabilityGapReport", "capability_gap_report"]


@dataclass(frozen=True)
class CapabilityGapLine:
    capability: str
    holders: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityGapReport:
    plan_ref: str
    node_count: int
    covered: tuple[CapabilityGapLine, ...]
    missing: tuple[CapabilityGapLine, ...]


def capability_gap_report(runtime: Runtime) -> CapabilityGapReport | str:
    plan = _active_plan(runtime)
    if isinstance(plan, str):
        return plan
    nodes = tuple(runtime.repos.nodes.get(node_id) for node_id in plan.node_ids)
    if not nodes:
        return f"没有可分析的 active Plan：{plan.ref} 没有节点"
    agents = tuple(runtime.repos.agents.all())
    order: list[str] = []
    used_by: dict[str, list[str]] = {}
    for node in nodes:
        for capability in node.required_capabilities:
            used_by.setdefault(capability, []).append(node.ref)
            if capability not in order:
                order.append(capability)
    covered: list[CapabilityGapLine] = []
    missing: list[CapabilityGapLine] = []
    for capability in order:
        line = CapabilityGapLine(
            capability=capability,
            holders=sum(1 for agent in agents if agent.holds(capability)),
            nodes=tuple(used_by[capability]),
        )
        if line.holders:
            covered.append(line)
        else:
            missing.append(line)
    return CapabilityGapReport(
        plan_ref=plan.ref,
        node_count=len(nodes),
        covered=tuple(covered),
        missing=tuple(missing),
    )


def _active_plan(runtime: Runtime) -> TaskPlan | str:
    if runtime.run is None:
        active = tuple(plan for plan in runtime.repos.plans.all() if plan.status == "ACTIVE")
        if len(active) == 1:
            return active[0]
        if not active:
            return "没有可分析的 active Plan"
        names = ", ".join(plan.ref for plan in active)
        return f"没有可分析的 active Plan：多个 ACTIVE Plan（{names}）"
    task = runtime.repos.tasks.get(runtime.run.task_id)
    if task.active_plan_id is None:
        return "没有可分析的 active Plan：当前运行没有 Plan"
    plan = runtime.repos.plans.get(task.active_plan_id)
    if plan.status != "ACTIVE":
        return f"没有可分析的 active Plan：{plan.ref} 状态是 {plan.status}"
    return plan
