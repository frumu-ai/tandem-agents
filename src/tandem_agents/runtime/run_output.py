from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.tandem_agents.core.repository.board import board_markdown, board_snapshot
from src.tandem_agents.runtime.runstate import blackboard_markdown, save_blackboard, update_status, write_status
from src.tandem_agents.runtime.artifact_store import mirror_run_file
from src.tandem_agents.utils.utils import atomic_write_text

logger = logging.getLogger("aca.runtime.run_output")


def _run_dir_for_path(path: Path) -> Path:
    parent = path.parent
    if parent.name in {"diffs", "logs", "artifacts", "worktrees"} and parent.parent != parent:
        return parent.parent
    return parent


def _mirror_run_output(path: Path) -> None:
    run_dir = _run_dir_for_path(path)
    logical_path = str(path.relative_to(run_dir))
    run_id = run_dir.name
    mirror_run_file(run_id, path, logical_path)


def write_board_snapshot(run_dir: Path, board: dict[str, Any]) -> None:
    snapshot = run_dir / "board.yaml"
    board_snapshot(board, snapshot)
    board_md = run_dir / "board.md"
    board_md.write_text(board_markdown(board), encoding="utf-8")
    _mirror_run_output(snapshot)
    _mirror_run_output(board_md)


def write_blackboard_snapshot(run_dir: Path, blackboard: dict[str, Any]) -> None:
    save_blackboard(run_dir / "blackboard.yaml", blackboard)
    blackboard_md = run_dir / "blackboard.md"
    blackboard_md.write_text(blackboard_markdown(blackboard), encoding="utf-8")
    _mirror_run_output(blackboard_md)


def set_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    phase: str | None = None,
    phase_detail: str | None = None,
    phase_role: str | None = None,
    run_status: str | None = None,
    blocker: tuple[bool, str | None, str | None, str | None] | None = None,
    metrics: dict[str, Any] | None = None,
    run_started: bool = False,
    run_completed: bool = False,
) -> dict[str, Any]:
    update_kwargs: dict[str, Any] = {}
    if phase is not None:
        update_kwargs["phase_name"] = phase
    if phase_detail is not None:
        update_kwargs["phase_detail"] = phase_detail
    if phase_role is not None:
        update_kwargs["phase_role"] = phase_role
    if run_status is not None:
        update_kwargs["run_status"] = run_status
    if blocker is not None:
        active, kind, message, owner_role = blocker
        update_kwargs["blocker_active"] = active
        update_kwargs["blocker_kind"] = kind
        update_kwargs["blocker_message"] = message
        update_kwargs["blocker_owner_role"] = owner_role
    if metrics is not None:
        update_kwargs["metrics"] = metrics
    if run_started:
        update_kwargs["run_started"] = True
    if run_completed:
        update_kwargs["run_completed"] = True
    updated = update_status(status, **update_kwargs)
    write_status(layout["status"], updated)
    return updated


def save_run_text(path: Path, content: str) -> None:
    atomic_write_text(path, content if content.endswith("\n") else content + "\n")
    try:
        _mirror_run_output(path)
    except Exception:
        # The local file is the source of truth — artifact-store mirroring is
        # advisory. Log loudly so a broken artifact-store config is visible
        # rather than silently dropping mirrored copies.
        logger.warning("Failed to mirror run output to artifact store: %s", path, exc_info=True)


def write_diff_snapshot(diffs_dir: Path, before_snapshot: str | None, after_snapshot: str | None) -> None:
    save_run_text(diffs_dir / "before.txt", before_snapshot or "(clean)")
    save_run_text(diffs_dir / "after.txt", after_snapshot or "(clean)")


def build_blocked_summary(
    *,
    task_title: str | None,
    message: str,
    worker_results: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = ["# Run blocked", ""]
    if task_title:
        lines.extend([f"Task: {task_title}", ""])
    lines.append(message)
    if worker_results:
        lines.extend(["", "## Workers", ""])
        lines.extend(f"- `{worker['worker_id']}` {worker['title']} - {worker['status']}" for worker in worker_results)
    return "\n".join(lines)


def build_completed_summary(
    *,
    run_id: str,
    task_title: str,
    repo_path: str,
    engine_label: str,
    provider_id: str,
    provider_model: str,
    worker_results: list[dict[str, Any]],
    review_returncode: int,
    test_returncode: int,
    diff_snapshot: str | None,
) -> str:
    lines: list[str] = [
        "# Run completed",
        "",
        f"- Run ID: `{run_id}`",
        f"- Task: {task_title}",
        f"- Repo: `{repo_path}`",
        f"- Engine: `{engine_label}`",
        f"- Provider: `{provider_id}` / `{provider_model}`",
        "",
        "## Workers",
        "",
    ]
    lines.extend(f"- `{worker['worker_id']}` {worker['title']} - {worker['status']}" for worker in worker_results)
    lines.extend(
        [
            "",
            "## Validation",
            "",
            f"- Review return code: `{review_returncode}`",
            f"- Test return code: `{test_returncode}`",
            "",
            "## Diff Snapshot",
            "",
            "```text",
            diff_snapshot or "(clean)",
            "```",
            "",
            "## Next Steps",
            "",
            "- If the repository still needs a commit or push, do that from the integrated repo checkout.",
        ]
    )
    return "\n".join(lines)
