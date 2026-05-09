from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.tandem_agents.config.config import ResolvedConfig
from src.tandem_agents.core.engine.engine import engine_status_report
from src.tandem_agents.runtime.state import (
    board_markdown,
    blackboard_markdown,
    load_board,
    load_blackboard,
    load_status,
)


def _parse_iso_to_epoch_seconds(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def alert_on_stale_blocked_runs(output_root: Path, threshold_minutes: int) -> int:
    """Scan all run directories under output_root and print a warning for any
    run that is in status 'blocked' and whose status.json has not been updated
    in the last <threshold_minutes>.

    Returns the number of stale blocked runs found.
    """
    if threshold_minutes <= 0:
        return 0
    if not output_root.exists():
        return 0
    threshold_seconds = threshold_minutes * 60
    now = time.time()
    stale_count = 0
    for run_dir in sorted(
        [p for p in output_root.iterdir() if p.is_dir() and p.name.startswith("run-")],
        key=lambda p: p.name,
    ):
        status_path = run_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            continue
        run_status = ((status.get("run") or {}).get("status") or "").strip()
        if run_status != "blocked":
            continue
        # Prefer the explicit run.updated_at field if present; fall back to mtime.
        updated_at = (status.get("run") or {}).get("updated_at")
        epoch = _parse_iso_to_epoch_seconds(updated_at)
        if epoch is None:
            try:
                epoch = status_path.stat().st_mtime
            except OSError:
                continue
        age_seconds = now - epoch
        if age_seconds < threshold_seconds:
            continue
        age_minutes = int(age_seconds // 60)
        blocker = (status.get("run") or {}).get("blocker") or {}
        kind = str(blocker.get("kind") or "unknown")
        message = str(blocker.get("message") or "").strip()
        sys.stderr.write(
            f"[BLOCKED >{age_minutes}m] run_id={run_dir.name} kind={kind}"
            + (f" message={message!r}" if message else "")
            + "\n"
        )
        stale_count += 1
    return stale_count


def latest_run_dir(output_root: Path) -> Path | None:
    if not output_root.exists():
        return None
    runs = sorted(
        [path for path in output_root.iterdir() if path.is_dir() and path.name.startswith("run-")],
        key=lambda path: path.name,
    )
    return runs[-1] if runs else None


def _print_section(title: str) -> None:
    print(f"\n== {title} ==")


def _load_yaml_text(path: Path) -> str:
    if not path.exists():
        return ""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        return yaml.safe_dump(loaded, sort_keys=False, allow_unicode=False)
    return path.read_text(encoding="utf-8")


def _tail_text(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(content[-lines:]).rstrip()


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _print_run_snapshot(run_dir: Path, tail_lines: int) -> None:
    status_path = run_dir / "status.json"
    summary_path = run_dir / "summary.md"
    events_path = run_dir / "events.jsonl"
    board_path = run_dir / "board.yaml"
    blackboard_path = run_dir / "blackboard.yaml"
    logs_dir = run_dir / "logs"

    _print_section("Run")
    print(f"Run directory: {run_dir}")
    print(f"Status file:   {status_path}")
    print(f"Summary file:  {summary_path}")
    print(f"Board file:    {board_path}")
    print(f"Blackboard:    {blackboard_path}")

    _print_section("Status")
    status = _load_json(status_path)
    if status is None:
        print("No status.json yet.")
    else:
        print(json.dumps(status, indent=2, sort_keys=False))

    _print_section("Engine")
    engine = (status or {}).get("engine") if isinstance(status, dict) else None
    if engine:
        print(json.dumps(engine, indent=2, sort_keys=False))
    else:
        print("No engine status in run file.")

    _print_section("Board")
    if board_path.exists():
        print(board_markdown(load_board(board_path)).rstrip())
    else:
        print("No board.yaml snapshot yet.")

    _print_section("Blackboard")
    if blackboard_path.exists():
        print(blackboard_markdown(load_blackboard(blackboard_path)).rstrip())
    else:
        print("No blackboard.yaml snapshot yet.")

    _print_section("Summary")
    if summary_path.exists():
        print(summary_path.read_text(encoding="utf-8").rstrip())
    else:
        print("No summary.md yet.")

    _print_section("Recent Events")
    if events_path.exists():
        print(_tail_text(events_path, tail_lines))
    else:
        print("No events.jsonl yet.")

    _print_section("Logs")
    if logs_dir.exists():
        files = sorted([path for path in logs_dir.iterdir() if path.is_file()], key=lambda path: path.name)
        if not files:
            print("No log files yet.")
        for path in files:
            print(f"\n--- {path.name} ---")
            print(_tail_text(path, tail_lines))
    else:
        print("No logs directory yet.")


def _print_engine_snapshot(cfg: ResolvedConfig) -> None:
    _print_section("Current Engine")
    try:
        report = engine_status_report(cfg)
        print(json.dumps(report, indent=2, sort_keys=False))
    except Exception as exc:  # noqa: BLE001
        print(f"Engine unreachable: {exc}")
        print("Note: Run monitor inside the container for live engine status:")
        print("  docker compose exec tandem-agents python3 -m src.tandem_agents.cli monitor")


def monitor_run(
    cfg: ResolvedConfig,
    run_dir: Path | None = None,
    follow: bool = False,
    tail_lines: int = 40,
    alert_on_blocked_after: int = 0,
) -> int:
    output_root = cfg.output_root()
    if alert_on_blocked_after and alert_on_blocked_after > 0:
        # Run the alert scan before the per-run snapshot so an operator running
        # `monitor.sh --alert-on-blocked-after 30` in cron-like contexts sees
        # the stale-run warnings up front, even if no specific run is supplied.
        alert_on_stale_blocked_runs(output_root, alert_on_blocked_after)
    run_dir = run_dir or latest_run_dir(output_root)
    if run_dir is None:
        print(f"No run directories found under {output_root}")
        return 1
    if not run_dir.exists():
        print(f"Run directory does not exist: {run_dir}")
        return 1

    _print_run_snapshot(run_dir, tail_lines)
    _print_engine_snapshot(cfg)

    if not follow:
        return 0

    print("\nFollowing updates. Press Ctrl+C to stop.")
    tracked_offsets: dict[Path, int] = {}
    tracked_mtimes: dict[Path, int] = {}
    status_path = run_dir / "status.json"
    events_path = run_dir / "events.jsonl"
    summary_path = run_dir / "summary.md"
    board_path = run_dir / "board.yaml"
    blackboard_path = run_dir / "blackboard.yaml"
    logs_dir = run_dir / "logs"
    for path in (status_path, summary_path, board_path, blackboard_path):
        if path.exists():
            tracked_mtimes[path] = path.stat().st_mtime_ns
    try:
        while True:
            for path, title, renderer in [
                (status_path, "Status", lambda p: json.dumps(_load_json(p), indent=2, sort_keys=False)),
                (summary_path, "Summary", lambda p: p.read_text(encoding="utf-8").rstrip()),
                (board_path, "Board", lambda p: board_markdown(load_board(p)).rstrip()),
                (blackboard_path, "Blackboard", lambda p: blackboard_markdown(load_blackboard(p)).rstrip()),
            ]:
                if path.exists():
                    mtime = path.stat().st_mtime_ns
                    if tracked_mtimes.get(path) != mtime:
                        tracked_mtimes[path] = mtime
                        _print_section(title)
                        print(renderer(path))
            if events_path.exists():
                tracked_offsets.setdefault(events_path, 0)
                _follow_file(events_path, tracked_offsets)
            if logs_dir.exists():
                for path in sorted([item for item in logs_dir.iterdir() if item.is_file()], key=lambda item: item.name):
                    tracked_offsets.setdefault(path, 0)
                    _follow_file(path, tracked_offsets)
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def _follow_file(path: Path, offsets: dict[Path, int]) -> None:
    current = offsets.get(path, 0)
    size = path.stat().st_size
    if size <= current:
        return
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(current)
        chunk = handle.read()
        offsets[path] = handle.tell()
    if not chunk:
        return
    title = path.name
    print(f"\n--- {title} update ---")
    print(chunk.rstrip())
