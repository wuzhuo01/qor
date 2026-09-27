"""CLI: accept one organizational objective and run it.

The default profile is the generic organization. It does not load a business
catalog and it does not fall back to a scripted plan. ``--profile example``
loads a local overlay explicitly.
"""

from __future__ import annotations

import argparse
import sys

from qor.adapters.agents.generic import GenericAgentRuntime
from qor.adapters.timeline import Timeline
from qor.app import build_app, resolve_planner
from qor.kernel.clock import SystemClock
from qor.ports.llm import LLMUnavailable
from qor.services.config import QORConfig
from qor.services.orchestrator import RunResult
from qor.services.runtime import Runtime

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one organizational objective.")
    parser.add_argument("--objective", required=True)
    parser.add_argument(
        "--profile",
        default="generic",
        choices=("generic", "example"),
        help=(
            "generic is the default organization and has no business catalog. "
            "example loads a local overlay explicitly."
        ),
    )
    parser.add_argument(
        "--planner",
        default="llm",
        choices=("llm", "stub"),
        help="llm fails when no provider is configured. stub is an explicit choice, not a fallback.",
    )
    parser.add_argument("--timeline", action="store_true", help="print the organization timeline")
    parser.add_argument("--audit", action="store_true", help="answer the ten audit questions")
    parser.add_argument("--transitions", action="store_true", help="print every StateTransition")
    parser.add_argument("--all", action="store_true", help="print everything")
    args = parser.parse_args(argv)

    config = QORConfig.load(profile=args.profile)
    if not config.plannable_capability_keys():
        print(
            "error: this profile has no plannable work capabilities. "
            "The runtime does not supply a business catalog. "
            "Provide a profile that declares work capabilities.",
            file=sys.stderr,
        )
        return 2
    try:
        planner = resolve_planner(args.planner)
    except LLMUnavailable as exc:
        print(f"error: planner=llm refused to fall back to stub: {exc}", file=sys.stderr)
        return 2
    clock = SystemClock()
    app = build_app(
        planner=planner,
        agents=GenericAgentRuntime(clock),
        clock=clock,
        config=config,
    )

    print(f"planner: {planner.kind} ({getattr(planner, 'provider', 'n/a')})")
    print(f"objective: {args.objective}\n")

    result = app.orchestrator.run_objective(args.objective)

    if args.timeline or args.all:
        _print_timeline(app.timeline, result)
    if args.transitions or args.all:
        _print_transitions(app.runtime, result)
    _print_summary(app.runtime, result)
    if args.audit or args.all:
        _print_audit(app.timeline, app.runtime, result)

    return 0 if result.completed else 1


def _print_timeline(timeline: Timeline, result: RunResult) -> None:
    print("=" * 78)
    print("ORGANIZATION TIMELINE")
    print("=" * 78)
    print(timeline.render(correlation_id=result.task.id))
    print()


def _print_transitions(runtime: Runtime, result: RunResult) -> None:
    print("=" * 78)
    print("STATE TRANSITIONS")
    print("=" * 78)
    for record in runtime.log.transitions():
        if record.correlation_id == result.task.id:
            print(f"  {record}")
    print()


def _print_summary(runtime: Runtime, result: RunResult) -> None:
    task = result.task
    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  task      {task.ref}  {task.status}")
    if result.plan:
        print(
            f"  plan      {result.plan.ref}  {result.plan.status}  "
            f"({len(result.plan.node_ids)} nodes, {len(result.plan.edges)} edges, "
            f"planner={result.plan.planner_kind}/{result.plan.planner_provider})"
        )
    for node_id in result.plan.topological_order() if result.plan else ():
        node = runtime.repos.nodes.get(node_id)
        print(
            f"    {node.ref} {node.status:<9} attempt {node.attempt}  "
            f"{node.node_kind:<9} {list(node.required_capabilities)}  {node.name}"
        )
    print(f"  artifacts {len(result.artifacts)}")
    for artifact in result.artifacts:
        print(
            f"    {artifact.ref} {artifact.status:<11} v{artifact.artifact_version} "
            f"{artifact.artifact_type.value:<23} "
            f"{len(artifact.findings)} finding(s)/{len(artifact.evidence)} evidence"
        )
    print(f"  reviews   {len(result.reviews)}")
    for review in result.reviews:
        print(
            f"    {review.ref} round {review.round} {review.status:<11} "
            f"{len(review.findings)} finding(s)"
        )
    if result.decision:
        print(
            f"  decision  {result.decision.ref} {result.decision.status} "
            f"{result.decision.decision_type.value}"
        )
        print(f"            {result.decision.statement}")
    for action in result.actions:
        print(
            f"  action    {action.ref} {action.status} dry_run={action.dry_run} "
            f"env={action.target.environment.value}"
        )
        for check in action.checks:
            print(f"            {check.kind:<11} {check.verdict:<7} {check.policy_ref}")
    print(
        f"  log       {len(runtime.log.transitions())} transitions, "
        f"{len(runtime.log.events())} events, {len(runtime.log.audit_records())} audit records"
    )
    print(f"  audit chain valid: {runtime.log.verify_audit_chain()}")
    if result.notes:
        print("  notes")
        for note in result.notes:
            print(f"    - {note}")
    print()


def _print_audit(timeline: Timeline, runtime: Runtime, result: RunResult) -> None:
    print("=" * 78)
    print("AUDIT QUESTIONS (requirement §13)")
    print("=" * 78)
    for question, answer in timeline.answer_audit_questions(runtime, result.task.id):
        print(f"  {question}")
        print(f"     {answer}")
    print()


if __name__ == "__main__":
    sys.exit(main())
