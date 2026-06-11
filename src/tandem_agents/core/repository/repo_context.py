from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import execute_engine_tool, list_engine_tool_ids
from src.tandem_agents.core.repository.repo_truth import repo_context_summary


@dataclass(frozen=True)
class RepoContextResult:
    text: str
    source: str
    fallback_used: bool
    error: str | None = None
    artifact_path: str | None = None
    index_source: str | None = None
    index_status: str | None = None
    index_error: str | None = None


def repo_context_for_task(
    cfg: ResolvedConfig,
    repo_path: Path,
    task: dict[str, Any] | None = None,
    *,
    artifact_path: Path | None = None,
    budget_chars: int = 6_000,
    limit: int = 12,
) -> RepoContextResult:
    """Return repo context for ACA planning, preferring Tandem repo intelligence."""

    task = task or {}
    fallback = repo_context_summary(repo_path, task, limit=limit)
    try:
        tool_ids = set(list_engine_tool_ids(cfg))
        if "repo.context_bundle" not in tool_ids:
            return RepoContextResult(
                text=_fallback_text(fallback, "repo.context_bundle tool is not available"),
                source="repo_truth",
                fallback_used=True,
                error="repo.context_bundle tool is not available",
            )
        index_status, index_error = _maybe_refresh_repo_index(cfg, repo_path, tool_ids)
        result = execute_engine_tool(
            cfg,
            "repo.context_bundle",
            {
                "repo_path": str(repo_path),
                "task": _task_query_text(task),
                "budget_chars": budget_chars,
                "required_files": _task_target_files(task),
                "limit": limit,
            },
        )
        payload = _structured_payload(result)
        if not isinstance(payload, dict):
            raise RuntimeError("repo.context_bundle did not return a structured object")
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        text = _format_context_bundle(payload, metadata)
        written_artifact = _write_artifact(
            artifact_path,
            {
                "source": "repo.context_bundle",
                "index_source": metadata.get("index_source"),
                "repo_path": str(repo_path),
                "task_id": task.get("task_id"),
                "tool_result": result,
                "bundle": payload,
            },
        )
        return RepoContextResult(
            text=text,
            source="repo.context_bundle",
            fallback_used=False,
            artifact_path=str(written_artifact) if written_artifact else None,
            index_source=str(metadata.get("index_source") or "") or None,
            index_status=index_status,
            index_error=index_error,
        )
    except Exception as exc:
        return RepoContextResult(
            text=_fallback_text(fallback, str(exc)),
            source="repo_truth",
            fallback_used=True,
            error=str(exc),
        )


def _maybe_refresh_repo_index(
    cfg: ResolvedConfig,
    repo_path: Path,
    tool_ids: set[str],
) -> tuple[str | None, str | None]:
    if "repo.index" not in tool_ids:
        return "skipped_tool_unavailable", None
    if not _repo_index_path_is_ignored(repo_path):
        return "skipped_unignored_store_path", ".tandem/repo-index.json is not ignored by this repo"
    try:
        execute_engine_tool(cfg, "repo.index", {"repo_path": str(repo_path)})
        return "refreshed", None
    except Exception as exc:
        return "refresh_failed", str(exc)


def _repo_index_path_is_ignored(repo_path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".tandem/repo-index.json"],
            cwd=repo_path,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return result.returncode == 0


def _task_query_text(task: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "description"):
        value = str(task.get(key) or "").strip()
        if value:
            parts.append(value)
    for item in task.get("acceptance_criteria") or []:
        value = str(item or "").strip()
        if value:
            parts.append(value)
    contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
    for key in ("local_goal", "program_goal"):
        value = str(contract.get(key) or "").strip()
        if value:
            parts.append(value)
    return "\n".join(parts).strip() or str(task.get("task_id") or "coding task")


def _task_target_files(task: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
    for source in (task, contract):
        for key in ("target_files", "files"):
            value = source.get(key)
            if isinstance(value, list):
                values.extend(value)
    seen: set[str] = set()
    files: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text.startswith("/") or text in seen:
            continue
        seen.add(text)
        files.append(text)
    return files


def _structured_payload(result: dict[str, Any]) -> Any:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if "structured" in metadata:
        return metadata["structured"]
    output = result.get("output")
    if isinstance(output, str) and output.strip():
        return json.loads(output)
    return output


def _format_context_bundle(bundle: dict[str, Any], metadata: dict[str, Any]) -> str:
    lines = [
        "Repo intelligence context bundle:",
        f"- Tool: {metadata.get('tool') or 'repo.context_bundle'}",
        f"- Index source: {metadata.get('index_source') or 'unknown'}",
    ]
    _append_path_list(lines, "Suggested first reads", bundle.get("suggested_first_reads"))
    _append_ranked_items(lines, "Likely files", bundle.get("likely_files"), ("file_path", "reason", "confidence"))
    _append_ranked_items(
        lines,
        "Relevant symbols",
        bundle.get("relevant_symbols"),
        ("symbol", "file_path", "kind", "confidence"),
    )
    _append_ranked_items(lines, "Graph evidence", bundle.get("graph_edges"), ("source", "relation", "target", "confidence"))
    _append_path_list(lines, "Likely tests", bundle.get("test_targets"))
    _append_path_list(lines, "Known gaps", bundle.get("gaps"))
    lines.append(
        "Use this bundle as discovery evidence only: read concrete files before editing or making final claims."
    )
    return "\n".join(line for line in lines if line is not None)


def _append_path_list(lines: list[str], label: str, values: Any) -> None:
    if not isinstance(values, list) or not values:
        return
    lines.append(f"{label}:")
    for value in values[:12]:
        text = str(value or "").strip()
        if text:
            lines.append(f"- {text}")


def _append_ranked_items(lines: list[str], label: str, values: Any, keys: tuple[str, ...]) -> None:
    if not isinstance(values, list) or not values:
        return
    lines.append(f"{label}:")
    for item in values[:12]:
        if not isinstance(item, dict):
            continue
        parts: list[str] = []
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(value)
        if parts:
            lines.append(f"- {' | '.join(parts)}")


def _fallback_text(fallback: str, reason: str) -> str:
    return "\n".join(
        [
            "Repo intelligence context bundle unavailable; using heuristic repo discovery.",
            f"Reason: {reason}",
            "",
            fallback,
        ]
    ).strip()


def _write_artifact(path: Path | None, payload: dict[str, Any]) -> Path | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
