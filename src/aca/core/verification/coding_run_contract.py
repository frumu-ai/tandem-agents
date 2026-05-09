from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodingRunContract:
    run_id: str
    task_title: str
    source_type: str
    repo_path: str
    branch_name: str
    worktree_path: str | None
    expected_repo_files: list[str] = field(default_factory=list)
    code_editing: bool = False
    requires_diff_review_before_handoff: bool = False
    requires_minimal_verification_before_handoff: bool = False
    handoff_mode: str = "task_only"
    handoff_rules: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_coding_run_contract(
    *,
    run_id: str,
    task: dict[str, Any],
    repo_path: Path,
    branch_name: str,
    expected_repo_files: list[str] | None = None,
    worktree_path: Path | None = None,
) -> CodingRunContract:
    normalized_expected_files = [
        str(path).strip()
        for path in (expected_repo_files or [])
        if str(path).strip()
    ]
    code_editing = bool(normalized_expected_files)
    handoff_rules: list[str]
    if code_editing:
        handoff_rules = [
            "Review the diff before handoff.",
            "Run at least one minimal verification command or deterministic repo validation before completion.",
            "Persist the diff snapshot and verification result in the run artifacts.",
        ]
    else:
        handoff_rules = [
            "No code-edit diff review is required for this run.",
            "Complete the run with a summary and the existing task artifacts.",
        ]
    return CodingRunContract(
        run_id=str(run_id),
        task_title=str(task.get("title") or "Task"),
        source_type=str((task.get("source") or {}).get("type") or ""),
        repo_path=str(repo_path),
        branch_name=str(branch_name),
        worktree_path=str(worktree_path) if worktree_path is not None else None,
        expected_repo_files=normalized_expected_files,
        code_editing=code_editing,
        requires_diff_review_before_handoff=code_editing,
        requires_minimal_verification_before_handoff=code_editing,
        handoff_mode="code_edit" if code_editing else "task_only",
        handoff_rules=handoff_rules,
    )
