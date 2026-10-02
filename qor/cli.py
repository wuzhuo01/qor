"""CLI: accept one organizational objective and run it.

The default profile is the generic organization. It does not load a business
catalog and it does not fall back to a scripted plan. ``--profile example``
loads a local overlay explicitly.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from qor.adapters.agents.generic import GenericAgentRuntime
from qor.adapters.persistence.run_record import RunRecord
from qor.adapters.planner.stub import StubPlanner
from qor.adapters.timeline import Timeline
from qor.app import QORApp, build_app, resolve_planner
from qor.companies import load_company
from qor.domain.enums import ArtifactType
from qor.domain.task import Task
from qor.kernel.clock import SystemClock
from qor.kernel.errors import EntryRejected
from qor.ports.llm import LLMUnavailable
from qor.services.capability_gap import CapabilityGapReport, capability_gap_report
from qor.services.config import QORConfig
from qor.services.orchestrator import RunResult
from qor.services.runtime import Runtime

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"start", "stop", "close", "gaps"}:
        return _company_command(args)
    parser = argparse.ArgumentParser(description="Run one organizational objective.")
    parser.add_argument("--objective")
    parser.add_argument(
        "--state",
        type=Path,
        help="organization snapshot. Startup loads it and does not run a task.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="save the loaded organization and exit without running a task",
    )
    parser.add_argument(
        "--stop-after-task",
        action="store_true",
        help="compatibility alias of --stop",
    )
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
    if args.stop or args.stop_after_task:
        if args.state is None:
            parser.error("--stop requires --state")
        return _stop_after_task(args.state, profile=args.profile)
    if args.state is not None and not args.objective:
        return _restore(args.state, stop=False, profile=args.profile)
    if not args.objective:
        parser.error("--objective is required")

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

    if args.state is not None:
        signal.signal(signal.SIGUSR1, lambda *_signum, _frame: app.request_stop_after_task())
        mark_task_executing(args.state)
    try:
        result = app.orchestrator.run_objective(args.objective)
    finally:
        if args.state is not None:
            clear_task_executing(args.state)

    if args.timeline or args.all:
        _print_timeline(app.timeline, result)
    if args.transitions or args.all:
        _print_transitions(app.runtime, result)
    _print_summary(app.runtime, result)
    if args.audit or args.all:
        _print_audit(app.timeline, app.runtime, result)
    if args.state is not None:
        app.stop(args.state)
        if not app.accept_next_task():
            print("stopped: current task finished")

    return 0 if result.completed else 1


def _company_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"qor {argv[0]}")
    parser.add_argument("--company")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--objective")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv[1:])
    if args.state is None:
        parser.error("--state is required")
    if argv[0] == "stop":
        return _stop_after_task(args.state, profile="generic")
    if argv[0] == "close":
        return _close_run(args.state)
    if argv[0] == "gaps":
        return _gaps(args.state)
    if not args.company:
        parser.error("--company is required")
    if args.resume and args.objective:
        print("error: --resume 不接受新目标", file=sys.stderr)
        return 2
    if args.resume:
        return _resume_company(args.company, args.state)
    if not args.objective:
        print("error: 新运行需要 --objective", file=sys.stderr)
        return 2
    return _start_company(args.company, args.state, args.objective)


def _start_company(name: str, state: Path, objective: str) -> int:
    """Start one company run from an objective. Uses the existing organization loop."""
    try:
        app = _company_app(name, state)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if company_pid(state) is not None or executing_pid(state) is not None:
        print("已有进程在运行，不能用新目标启动", file=sys.stderr)
        return 2
    run = app.runtime.run
    if run is not None and run.status != "closed":
        print("已有运行，不能用新目标启动", file=sys.stderr)
        return 2
    if run is not None:
        app.runtime.run_history.append(run)
        app.runtime.run = None
    return _serve(app, state, objective=objective, task=None)


def _resume_company(name: str, state: Path) -> int:
    """Continue the unfinished run in this state file. Does not accept a new objective."""
    if not state.is_file():
        print("无可恢复运行", file=sys.stderr)
        return 2
    try:
        app = _company_app(name, state)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run = app.runtime.run
    if run is None or run.status == "closed":
        print("无可恢复运行", file=sys.stderr)
        return 2
    task = app.runtime.repos.tasks.get(run.task_id)
    if _task_is_terminal(app, task):
        print("无可恢复运行", file=sys.stderr)
        return 2
    if company_pid(state) is not None or executing_pid(state) is not None:
        print("已有进程在运行", file=sys.stderr)
        return 2
    return _serve(app, state, objective=run.objective, task=task)


def _company_app(name: str, state: Path) -> QORApp:
    config = load_company(name)
    return build_app(
        planner=StubPlanner(),
        agents=GenericAgentRuntime(SystemClock()),
        clock=SystemClock(),
        config=config,
        state_path=state if state.is_file() else None,
    )


def _serve(app: QORApp, state: Path, *, objective: str, task: Task | None) -> int:
    organization = app.runtime.organization()
    print(f"company: {organization.name}")
    print(f"state: {state}")
    print(f"objective: {objective}")
    sys.stdout.flush()

    def _on_stop(_signum: int, _frame: object) -> None:
        app.request_stop_after_task()

    signal.signal(signal.SIGUSR1, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    mark_company_alive(state)
    try:
        if task is None:
            requester = app.runtime.agent_index[app.runtime.config.organization["human_cio"]]
            task = app.orchestrator.intake.submit(
                objective=objective,
                requested_by_agent_id=requester,
                required_outputs=(ArtifactType.SYNTHESIS, ArtifactType.RECOMMENDATION),
            )
        _remember_run(app, objective, task)
        app.stop(state)
        print(f"task: {task.ref} {task.status} {task.id}")
        sys.stdout.flush()
        mark_task_executing(state)
        hold = state.with_name(state.name + ".hold")
        if hold.is_file():
            while app.accept_next_task():
                try:
                    time.sleep(0.05)
                except InterruptedError:
                    continue
        refused = False
        try:
            result = app.orchestrator.run_task(task)
            task = result.task
        except EntryRejected as exc:
            task = app.runtime.repos.tasks.get(task.id)
            print(f"error: {exc}", file=sys.stderr)
            refused = True
        except Exception as exc:
            task = app.runtime.repos.tasks.get(task.id)
            print(f"error: {exc}", file=sys.stderr)
        finally:
            clear_task_executing(state)
            _remember_run(app, objective, task)
            app.stop(state)
        if refused:
            return 2
        print(f"task_status: {task.status}")
        if task.status == "BLOCKED":
            _print_blocked(app.runtime, task)
        print("execution: finished")
        sys.stdout.flush()
        if not app.accept_next_task():
            print("stopped: saved")
            sys.stdout.flush()
            return 0
        while app.accept_next_task():
            try:
                time.sleep(0.2)
            except InterruptedError:
                continue
        app.stop(state)
        print("stopped: saved")
        sys.stdout.flush()
        return 0
    finally:
        clear_company_alive(state)
        clear_task_executing(state)


def _print_blocked(runtime: Runtime, task: Task) -> None:
    """Print the block record already stored on qor.task.blocked."""
    payload: dict[str, object] = {}
    for event in runtime.log.events():
        if event.name == "qor.task.blocked" and event.aggregate_id == task.id:
            payload = event.payload
    print(f"blocked_reason: {payload.get('blocked_reason', 'none')}")
    print(f"blocked_task: {payload.get('blocked_task', 'none')}")
    print(f"required_capability: {payload.get('required_capability', 'not_applicable')}")
    print(f"assigned_agent: {payload.get('assigned_agent', 'none')}")
    print(f"reason: {payload.get('reason', 'none')}")
    print(f"next_action: {payload.get('next_action', 'not_applicable')}")


def _remember_run(app: QORApp, objective: str, task: Task) -> None:
    app.runtime.run = RunRecord(
        objective=objective,
        task_id=task.id,
        status="completed" if _task_is_terminal(app, task) else "active",
    )


def _task_is_terminal(app: QORApp, task: Task) -> bool:
    return app.runtime.machines.for_entity(task.entity_type).is_terminal(task.status)


def alive_marker(state: Path) -> Path:
    return state.with_name(state.name + ".alive")


def mark_company_alive(state: Path) -> None:
    alive_marker(state).write_text(str(os.getpid()), encoding="utf-8")


def clear_company_alive(state: Path) -> None:
    marker = alive_marker(state)
    if marker.is_file():
        marker.unlink()


def company_pid(state: Path) -> int | None:
    return _pid_from(alive_marker(state))


def running_marker(state: Path) -> Path:
    return state.with_name(state.name + ".running")


def mark_task_executing(state: Path) -> None:
    running_marker(state).write_text(str(os.getpid()), encoding="utf-8")


def clear_task_executing(state: Path) -> None:
    marker = running_marker(state)
    if marker.is_file():
        marker.unlink()


def executing_pid(state: Path) -> int | None:
    return _pid_from(running_marker(state))


def _pid_from(marker: Path) -> int | None:
    if not marker.is_file():
        return None
    try:
        pid = int(marker.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    if pid == os.getpid() or not _pid_alive(pid):
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _gaps(path: Path) -> int:
    """Print capability coverage for the current active plan. Does not save."""
    if not path.is_file():
        print("没有可分析的 active Plan：状态文件不存在", file=sys.stderr)
        return 2
    app = build_app(
        planner=StubPlanner(),
        agents=GenericAgentRuntime(SystemClock()),
        clock=SystemClock(),
        config=QORConfig.load(profile="generic"),
        state_path=path,
    )
    report = capability_gap_report(app.runtime)
    if isinstance(report, str):
        print(report, file=sys.stderr)
        return 2
    _print_gap_report(report)
    return 0


def _print_gap_report(report: CapabilityGapReport) -> None:
    print(f"plan: {report.plan_ref}")
    print(f"nodes: {report.node_count}")
    print("covered:")
    for line in report.covered:
        print(f"- {line.capability} holders={line.holders} nodes={','.join(line.nodes)}")
    print("missing:")
    for line in report.missing:
        print(f"- {line.capability} holders={line.holders} nodes={','.join(line.nodes)}")


def _close_run(path: Path) -> int:
    """Mark the current unfinished run closed. Does not run a task or call a model."""
    if executing_pid(path) is not None or company_pid(path) is not None:
        print("请先使用 stop 安全停止", file=sys.stderr)
        return 2
    if not path.is_file():
        print("无可关闭运行", file=sys.stderr)
        return 2
    app = build_app(
        planner=StubPlanner(),
        agents=GenericAgentRuntime(SystemClock()),
        clock=SystemClock(),
        config=QORConfig.load(profile="generic"),
        state_path=path,
    )
    run = app.runtime.run
    if run is None:
        print("无可关闭运行", file=sys.stderr)
        return 2
    if run.status == "closed":
        print("已关闭")
        return 0
    task = app.runtime.repos.tasks.get(run.task_id)
    if run.status == "completed" or _task_is_terminal(app, task):
        print("该 Run 已完成", file=sys.stderr)
        return 2
    app.runtime.run = RunRecord(objective=run.objective, task_id=run.task_id, status="closed")
    app.stop(path)
    print("run: CLOSED")
    return 0


def _stop_after_task(path: Path, *, profile: str) -> int:
    """Deliver stop-after-task. Does not start a task or call a model."""
    task_pid = executing_pid(path)
    pid = task_pid or company_pid(path)
    if pid is not None:
        os.kill(pid, signal.SIGUSR1)
        if task_pid is not None:
            print("stop requested after current task")
        else:
            print("stop requested")
        return 0
    if not path.is_file():
        print("stopped: no current task")
        return 0
    app = build_app(
        planner=StubPlanner(),
        agents=GenericAgentRuntime(SystemClock()),
        clock=SystemClock(),
        config=QORConfig.load(profile=profile),
        state_path=path,
    )
    app.stop(path)
    print("stopped: no current task")
    return 0


def _restore(path: Path | None, *, stop: bool, profile: str) -> int:
    """Load a saved organization. Does not run, continue, or create a task."""
    if path is None or not path.is_file():
        if stop:
            print("stopped: no state to save")
            return 0
        print("error: no saved organization state", file=sys.stderr)
        return 2
    app = build_app(
        planner=StubPlanner(),
        agents=GenericAgentRuntime(SystemClock()),
        clock=SystemClock(),
        config=QORConfig.load(profile=profile),
        state_path=path,
    )
    _print_restored(app.runtime)
    if stop:
        app.stop(path)
        print("stopped: saved")
    return 0


def _print_restored(runtime: Runtime) -> None:
    print("state: loaded")
    tasks = runtime.repos.tasks.all()
    print(f"tasks: {len(tasks)}")
    for task in tasks:
        print(f"{task.ref} {task.status} {task.id}")
    print(f"agents: {len(runtime.repos.agents.all())}")
    print(f"transitions: {len(runtime.log.transitions())}")
    print(f"events: {len(runtime.log.events())}")
    print(f"audit: {len(runtime.log.audit_records())}")
    print(f"artifacts: {len(runtime.repos.artifacts.all())}")
    print(f"decisions: {len(runtime.repos.decisions.all())}")
    print(f"llm_calls: {len(runtime.llm_calls)}")
    for call in runtime.llm_calls:
        print(
            f"{call.task_ref} {call.model} {call.input_tokens} {call.output_tokens} "
            f"{call.occurred_at.isoformat()}"
        )


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
