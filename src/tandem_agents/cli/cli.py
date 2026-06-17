#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.tandem_agents.config.config import ResolvedConfig, resolve_config, validate_config
from src.tandem_agents.core.coordination.coordination import CoordinationStore
from src.tandem_agents.core.coordination.coordination_reaper import (
    ReaperThreadHandle,
    coordination_reaper_tick,
    start_reaper_thread,
)
from src.tandem_agents.core.engine.engine import engine_status_report
from src.tandem_agents.core.execution.runtime_entrypoints import run_worker
from src.tandem_agents.core.repository.repo_graph_eval import run_repo_graph_eval
from src.tandem_agents.core.repository.repository import repository_binding_issues
from src.tandem_agents.core.scheduling.outbox_dispatcher import dispatch_outbox_tick, run_outbox_dispatcher
from src.tandem_agents.core.scheduling.scheduler import plan_task_admissions, scheduler_snapshot
from src.tandem_agents.core.scheduling.scheduler_dispatcher import dispatch_scheduled_runs
from src.tandem_agents.cli.monitor import monitor_run
from src.tandem_agents.cli.runner import run_once
from src.tandem_agents.cli.dogfood import run_linear_graph_dogfood
from src.tandem_agents.runtime.operator_view import build_operator_summary
from src.tandem_agents.runtime.state import board_summary, ensure_board_template, load_board
from src.tandem_agents.runtime.workspace_registry import load_workspace, save_workspace, set_active_project, workspace_summary
from src.tandem_agents.runtime.task_sources import preview_task


def _root_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _load_cfg(root: Path, *, coordination_role: str | None = None) -> ResolvedConfig:
    env: dict[str, str] = {}
    if coordination_role:
        env["ACA_COORDINATION_ROLE"] = coordination_role
    return resolve_config(root, env=env or None)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False))


def _validate_runtime(cfg: ResolvedConfig) -> list[str]:
    errors = list(validate_config(cfg))
    errors.extend(repository_binding_issues(cfg))
    if cfg.task_source.type == "kanban_board":
        board_path = cfg.task_source_path()
        if board_path and not board_path.exists():
            errors.append(f"Kanban board file does not exist: {board_path}")
    return errors


def cmd_validate(cfg: ResolvedConfig) -> int:
    summary = cfg.config_summary()
    print("Resolved config:")
    _print_json(summary)
    print()
    engine = engine_status_report(cfg)
    print("Engine:")
    _print_json(engine)
    print()
    errors = _validate_runtime(cfg)
    if errors:
        print("Validation errors:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validation complete.")
    return 0


def cmd_check_engine(cfg: ResolvedConfig) -> int:
    report = engine_status_report(cfg)
    _print_json(report)
    if report.get("status") in {"missing", "blocked"}:
        return 1
    return 0


def cmd_print_config(cfg: ResolvedConfig) -> int:
    _print_json(cfg.config_summary())
    return 0


def cmd_repo_graph_eval(_cfg: ResolvedConfig) -> int:
    result = run_repo_graph_eval()
    _print_json(result)
    return 0 if result.get("passed") else 1


def cmd_init_board(cfg: ResolvedConfig) -> int:
    board_path = cfg.task_source_path()
    if cfg.task_source.type != "kanban_board" or board_path is None:
        board_path = cfg.root_dir / "config" / "board.yaml"
    board = ensure_board_template(board_path)
    print(f"Board template ready: {board_path}")
    _print_json(board_summary(board))
    return 0


def _exit_code_for_run_status(run_status: str) -> int:
    """Map a terminal run-status string to a CLI exit code.

    0 = completed (success)
    1 = blocked   (run finished but outcome was negative — operator action needed)
    2 = error     (internal failure / crash — distinct from a clean block)

    Operators and CI can branch on these without parsing JSON.
    """
    status = (run_status or "").strip()
    if status == "completed":
        return 0
    if status == "blocked":
        return 1
    return 2


def _maybe_start_reaper(cfg: ResolvedConfig) -> ReaperThreadHandle | None:
    """Start the coordination reaper in a background thread if coordination is
    enabled. Returns the handle for shutdown, or None if the reaper should not
    run (no coordination, or feature flag off)."""
    if not bool(getattr(cfg.coordination, "enabled", True)):
        return None
    try:
        return start_reaper_thread(cfg)
    except Exception:
        # Don't let reaper startup failure block a run; the run can still
        # proceed and the API/operator can reap manually if needed.
        import logging

        logging.getLogger("aca.cli").warning(
            "Failed to start coordination reaper; runs will rely on TTL expiration alone.",
            exc_info=True,
        )
        return None


def cmd_run(cfg: ResolvedConfig, dry_run: bool = False) -> int:
    errors = _validate_runtime(cfg)
    if errors:
        print("ACA cannot start because the runtime config is incomplete:")
        for error in errors:
            print(f"- {error}")
        return 1
    if dry_run or cfg.agent.dry_run:
        print("Dry run requested.")
        return cmd_validate(cfg)
    reaper = _maybe_start_reaper(cfg)
    try:
        result = run_once(cfg)
    finally:
        if reaper is not None:
            reaper.stop()
    run_status = str(result.get("status", {}).get("run", {}).get("status") or "")
    _print_json({"run_id": result.get("run_id"), "status": run_status})
    print(f"Run directory: {result.get('layout', {}).get('run_dir')}")
    return _exit_code_for_run_status(run_status)


def cmd_worker(cfg: ResolvedConfig, dry_run: bool = False) -> int:
    errors = _validate_runtime(cfg)
    if errors:
        print("ACA cannot start worker mode because the runtime config is incomplete:")
        for error in errors:
            print(f"- {error}")
        return 1
    if dry_run or cfg.agent.dry_run:
        print("Dry run requested.")
        return cmd_validate(cfg)
    reaper = _maybe_start_reaper(cfg)
    try:
        result = run_worker(cfg)
    finally:
        if reaper is not None:
            reaper.stop()
    run_status = str(result.get("status", {}).get("run", {}).get("status") or "")
    _print_json({"run_id": result.get("run_id"), "status": run_status})
    print(f"Run directory: {result.get('layout', {}).get('run_dir')}")
    return _exit_code_for_run_status(run_status)


def cmd_next_task(cfg: ResolvedConfig) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
        store.ensure_schema()
        result = preview_task(cfg, coordination=store)
        print("=== Next Task Preview ===")
        _print_json(result)
        return 0
    except RuntimeError as e:
        print(f"No eligible task: {e}")
        return 1


def cmd_workspace(cfg: ResolvedConfig, limit: int = 25, set_active: str | None = None) -> int:
    root = cfg.root_dir
    workspace = load_workspace(root)
    if set_active is not None:
        try:
            workspace = set_active_project(workspace, set_active)
        except ValueError as exc:
            print(f"Workspace update failed: {exc}")
            return 1
        save_workspace(root, workspace)
    view = workspace_summary(workspace)
    if limit > 0:
        view["projects"] = view.get("projects", [])[:limit]
        view["runs"] = view.get("runs", [])[:limit]
    _print_json(view)
    return 0


def cmd_monitor(
    cfg: ResolvedConfig,
    run_dir: str | None = None,
    follow: bool = False,
    tail_lines: int = 40,
    alert_on_blocked_after: int = 0,
) -> int:
    path = Path(run_dir).expanduser() if run_dir else None
    if path is not None and not path.is_absolute():
        path = (cfg.root_dir / path).resolve()
    return monitor_run(
        cfg,
        path,
        follow=follow,
        tail_lines=tail_lines,
        alert_on_blocked_after=alert_on_blocked_after,
    )


def cmd_coordination_status(cfg: ResolvedConfig, limit: int = 25) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    _print_json(store.snapshot(limit=max(1, min(limit, 100))))
    return 0


def cmd_coordination_workers(cfg: ResolvedConfig, limit: int = 25) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    snapshot = store.snapshot(limit=max(1, min(limit, 100)))
    _print_json(
        {
            "db_path": snapshot["db_path"],
            "workers": store.list_workers(limit=max(1, min(limit, 100))),
            "summary": snapshot.get("summary", {}),
        }
    )
    return 0


def cmd_operator_status(cfg: ResolvedConfig, limit: int = 25) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    _print_json(build_operator_summary(cfg, coordination=store, limit=max(1, min(limit, 100))))
    return 0


def cmd_outbox_dispatcher(
    cfg: ResolvedConfig,
    *,
    once: bool = False,
    limit: int = 25,
) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    if once:
        summary = dispatch_outbox_tick(cfg, coordination=store, limit=limit)
        _print_json(summary)
        return 0
    print("ACA outbox dispatcher started.")
    try:
        run_outbox_dispatcher(cfg, coordination=store, limit=limit)
    except KeyboardInterrupt:
        print("ACA outbox dispatcher stopped.")
        return 0


def cmd_scheduler_plan(cfg: ResolvedConfig, limit: int = 25) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    snapshot = scheduler_snapshot(cfg, coordination=store, limit=max(1, min(limit, 100)))
    plan = plan_task_admissions(cfg, coordination=store, limit=max(1, min(limit, 100)))
    _print_json({"snapshot": snapshot, "plan": plan})
    return 0


def cmd_lease_list(cfg: ResolvedConfig, status: str | None, task_key: str | None, limit: int) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    leases = store.list_leases(status=status, task_key=task_key, limit=limit)
    _print_json({"count": len(leases), "leases": leases})
    return 0


def cmd_lease_release(cfg: ResolvedConfig, lease_id: str, reason: str | None, status: str = "stale") -> int:
    if not lease_id.strip():
        print("lease_id is required.")
        return 2
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    released = store.release_lease(
        lease_id.strip(),
        status=status,
        reason=(reason or "manual release via CLI"),
    )
    if released is None:
        print(f"Lease {lease_id!r} not found.")
        return 1
    _print_json({"released": released})
    return 0


def cmd_lease_reap(cfg: ResolvedConfig) -> int:
    try:
        reaped = coordination_reaper_tick(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    _print_json({"count": len(reaped), "reaped": reaped})
    return 0


def cmd_blackboard(cfg: ResolvedConfig, run_id: str, json_output: bool, phase: str | None) -> int:
    """Pretty-print or dump the blackboard for a single run.

    Reads runs/<run_id>/blackboard.yaml. With --phase, returns just the entries
    for that phase (matched against blackboard["events"][i]["phase"]).
    """
    if not run_id.strip():
        print("run_id is required.")
        return 2
    runs_root = cfg.output_root() if hasattr(cfg, "output_root") else (cfg.root_dir / "runs")
    run_dir = runs_root / run_id.strip()
    bb_path = run_dir / "blackboard.yaml"
    if not bb_path.exists():
        print(f"Blackboard not found: {bb_path}")
        return 1
    import yaml  # local import; pyyaml is a project dep

    try:
        payload = yaml.safe_load(bb_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        print(f"Failed to parse blackboard YAML: {exc}")
        return 1
    if phase:
        events = payload.get("events") or []
        if isinstance(events, list):
            payload["events"] = [e for e in events if isinstance(e, dict) and str(e.get("phase") or "") == phase]
    if json_output:
        _print_json(payload)
    else:
        print(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    return 0


def cmd_scheduler_dispatch(cfg: ResolvedConfig, limit: int = 25, wait: bool = True) -> int:
    try:
        store = CoordinationStore.from_config(cfg)
    except RuntimeError as exc:
        print(f"Coordination storage unavailable: {exc}")
        return 1
    store.ensure_schema()
    result = dispatch_scheduled_runs(cfg, coordination=store, limit=max(1, min(limit, 100)), wait=wait)
    _print_json(result)
    return 0


def cmd_dogfood_linear_graph(
    cfg: ResolvedConfig,
    *,
    api_url: str,
    project_slug: str,
    item: str | None,
    token_file: str | None,
    wait_seconds: int,
    trigger_timeout_seconds: float | None,
    allow_fallback: bool,
) -> int:
    try:
        code, summary = run_linear_graph_dogfood(
            root=cfg.root_dir,
            api_url=api_url,
            project_slug=project_slug,
            item=item,
            token_file=Path(token_file).expanduser() if token_file else None,
            wait_seconds=wait_seconds,
            trigger_timeout_seconds=trigger_timeout_seconds,
            expect_graph=not allow_fallback,
        )
    except Exception as exc:
        print(f"ACA Linear graph dogfood failed: {exc}", file=sys.stderr)
        return 1
    _print_json(summary)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aca")
    parser.add_argument("--root", default=str(_root_dir()), help="ACA workspace root")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate")
    validate.set_defaults(command="validate")

    check_engine = sub.add_parser("check-engine")
    check_engine.set_defaults(command="check-engine")

    print_config = sub.add_parser("print-config")
    print_config.set_defaults(command="print-config")

    init_board = sub.add_parser("init-board")
    init_board.set_defaults(command="init-board")

    next_task = sub.add_parser("next-task")
    next_task.set_defaults(command="next-task")

    workspace = sub.add_parser("workspace")
    workspace.add_argument("--limit", type=int, default=25)
    workspace.add_argument("--set-active")
    workspace.set_defaults(command="workspace")

    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(command="run")

    worker = sub.add_parser("worker")
    worker.add_argument("--dry-run", action="store_true")
    worker.set_defaults(command="worker")

    monitor = sub.add_parser("monitor")
    monitor.add_argument("--run-dir")
    monitor.add_argument("--follow", action="store_true")
    monitor.add_argument("--tail-lines", type=int, default=40)
    monitor.add_argument(
        "--alert-on-blocked-after",
        type=int,
        default=0,
        metavar="MINUTES",
        help=(
            "Print a stderr warning for every run whose status is 'blocked' and "
            "whose status.json has not been updated in the last MINUTES minutes. "
            "Useful for cron-like wrappers around scripts/monitor.sh. 0 disables."
        ),
    )
    monitor.set_defaults(command="monitor")

    coordination = sub.add_parser("coordination-status")
    coordination.add_argument("--limit", type=int, default=25)
    coordination.set_defaults(command="coordination-status")

    worker_registry = sub.add_parser("coordination-workers")
    worker_registry.add_argument("--limit", type=int, default=25)
    worker_registry.set_defaults(command="coordination-workers")

    operator = sub.add_parser("operator-status")
    operator.add_argument("--limit", type=int, default=25)
    operator.set_defaults(command="operator-status")

    outbox = sub.add_parser("outbox-dispatcher")
    outbox.add_argument("--once", action="store_true")
    outbox.add_argument("--limit", type=int, default=25)
    outbox.set_defaults(command="outbox-dispatcher")

    scheduler = sub.add_parser("scheduler-plan")
    scheduler.add_argument("--limit", type=int, default=25)
    scheduler.set_defaults(command="scheduler-plan")

    scheduler_dispatch = sub.add_parser("scheduler-dispatch")
    scheduler_dispatch.add_argument("--limit", type=int, default=25)
    scheduler_dispatch.add_argument("--no-wait", action="store_true")
    scheduler_dispatch.set_defaults(command="scheduler-dispatch")

    dogfood_linear_graph = sub.add_parser(
        "dogfood-linear-graph",
        help="Trigger one Linear-backed ACA run and assert planning used repo.context_bundle.",
    )
    dogfood_linear_graph.add_argument("--api-url", default="http://127.0.0.1:39735")
    dogfood_linear_graph.add_argument("--project-slug", required=True)
    dogfood_linear_graph.add_argument("--item", default=None, help="Linear issue identifier/id/url to trigger.")
    dogfood_linear_graph.add_argument("--token-file", default=None)
    dogfood_linear_graph.add_argument("--wait-seconds", type=int, default=180)
    dogfood_linear_graph.add_argument("--trigger-timeout-seconds", type=float, default=None)
    dogfood_linear_graph.add_argument("--allow-fallback", action="store_true")
    dogfood_linear_graph.set_defaults(command="dogfood-linear-graph")

    repo_graph_eval = sub.add_parser(
        "repo-graph-eval",
        help="Run deterministic ACA repo graph routing eval fixtures.",
    )
    repo_graph_eval.set_defaults(command="repo-graph-eval")

    # `aca lease ...` — operator-side lease inspection / unblock
    lease_parser = sub.add_parser("lease", help="Inspect and manage coordination leases.")
    lease_sub = lease_parser.add_subparsers(dest="lease_command")
    lease_list = lease_sub.add_parser("list", help="List leases (default: all).")
    lease_list.add_argument(
        "--status",
        choices=["all", "active", "expired", "stale", "completed", "blocked", "failed"],
        default="all",
    )
    lease_list.add_argument("--task-key", default=None)
    lease_list.add_argument("--limit", type=int, default=50)
    lease_list.set_defaults(lease_command="list")
    lease_release = lease_sub.add_parser(
        "release",
        help="Manually release a lease (e.g. after a worker crash that the reaper hasn't caught yet).",
    )
    lease_release.add_argument("lease_id")
    lease_release.add_argument("--reason", default=None)
    lease_release.add_argument(
        "--status",
        choices=["stale", "blocked", "failed", "completed"],
        default="stale",
        help="Terminal lease status to record. Defaults to stale so manual unblocks do not mark unfinished tasks done.",
    )
    lease_release.set_defaults(lease_command="release")
    lease_reap = lease_sub.add_parser(
        "reap-stale",
        help="One-shot run of the coordination reaper. Useful when the API server is down.",
    )
    lease_reap.set_defaults(lease_command="reap-stale")
    lease_parser.set_defaults(command="lease")

    # `aca blackboard <run_id>` — dump the blackboard for offline triage
    blackboard = sub.add_parser("blackboard", help="Dump the blackboard for a run.")
    blackboard.add_argument("run_id")
    blackboard.add_argument("--json", dest="json_output", action="store_true")
    blackboard.add_argument("--phase", default=None, help="Filter events to this phase only.")
    blackboard.set_defaults(command="blackboard")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    role = "worker" if args.command == "worker" else None
    cfg = _load_cfg(Path(args.root), coordination_role=role)

    command = args.command or "run"
    if command == "validate":
        return cmd_validate(cfg)
    if command == "check-engine":
        return cmd_check_engine(cfg)
    if command == "print-config":
        return cmd_print_config(cfg)
    if command == "repo-graph-eval":
        return cmd_repo_graph_eval(cfg)
    if command == "init-board":
        return cmd_init_board(cfg)
    if command == "next-task":
        return cmd_next_task(cfg)
    if command == "workspace":
        return cmd_workspace(cfg, limit=getattr(args, "limit", 25), set_active=getattr(args, "set_active", None))
    if command == "monitor":
        return cmd_monitor(
            cfg,
            run_dir=getattr(args, "run_dir", None),
            follow=getattr(args, "follow", False),
            tail_lines=getattr(args, "tail_lines", 40),
            alert_on_blocked_after=getattr(args, "alert_on_blocked_after", 0),
        )
    if command == "coordination-status":
        return cmd_coordination_status(cfg, limit=getattr(args, "limit", 25))
    if command == "coordination-workers":
        return cmd_coordination_workers(cfg, limit=getattr(args, "limit", 25))
    if command == "operator-status":
        return cmd_operator_status(cfg, limit=getattr(args, "limit", 25))
    if command == "outbox-dispatcher":
        return cmd_outbox_dispatcher(cfg, once=getattr(args, "once", False), limit=getattr(args, "limit", 25))
    if command == "scheduler-plan":
        return cmd_scheduler_plan(cfg, limit=getattr(args, "limit", 25))
    if command == "scheduler-dispatch":
        return cmd_scheduler_dispatch(cfg, limit=getattr(args, "limit", 25), wait=not getattr(args, "no_wait", False))
    if command == "dogfood-linear-graph":
        return cmd_dogfood_linear_graph(
            cfg,
            api_url=getattr(args, "api_url", "http://127.0.0.1:39735"),
            project_slug=getattr(args, "project_slug"),
            item=getattr(args, "item", None),
            token_file=getattr(args, "token_file", None),
            wait_seconds=getattr(args, "wait_seconds", 180),
            trigger_timeout_seconds=getattr(args, "trigger_timeout_seconds", None),
            allow_fallback=getattr(args, "allow_fallback", False),
        )
    if command == "run":
        return cmd_run(cfg, dry_run=getattr(args, "dry_run", False))
    if command == "worker":
        return cmd_worker(cfg, dry_run=getattr(args, "dry_run", False))
    if command == "lease":
        lease_command = getattr(args, "lease_command", None) or "list"
        if lease_command == "list":
            return cmd_lease_list(
                cfg,
                status=getattr(args, "status", "all"),
                task_key=getattr(args, "task_key", None),
                limit=getattr(args, "limit", 50),
            )
        if lease_command == "release":
            return cmd_lease_release(
                cfg,
                lease_id=getattr(args, "lease_id"),
                reason=getattr(args, "reason", None),
                status=getattr(args, "status", "stale"),
            )
        if lease_command == "reap-stale":
            return cmd_lease_reap(cfg)
        parser.error(f"Unknown lease command: {lease_command}")
        return 2
    if command == "blackboard":
        return cmd_blackboard(
            cfg,
            run_id=getattr(args, "run_id", ""),
            json_output=getattr(args, "json_output", False),
            phase=getattr(args, "phase", None),
        )
    parser.error(f"Unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
