from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.engine.engine import engine_visible_path, execute_engine_tool, list_engine_tool_ids
from src.tandem_agents.core.repository.repo_truth import repo_context_summary


@dataclass(frozen=True)
class RepoContextResult:
    text: str
    source: str
    fallback_used: bool
    error: str | None = None
    artifact_path: str | None = None
    path_scope: str | None = None
    required_files: list[str] | None = None
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
    context_hints = repo_context_hints_for_task(task)
    fallback = repo_context_summary(repo_path, task, limit=limit)
    try:
        tool_ids = set(list_engine_tool_ids(cfg))
        if "repo.context_bundle" not in tool_ids:
            return RepoContextResult(
                text=_fallback_text(fallback, "repo.context_bundle tool is not available"),
                source="repo_truth",
                fallback_used=True,
                error="repo.context_bundle tool is not available",
                path_scope=str(context_hints.get("path_scope") or "."),
                required_files=list(context_hints.get("required_files") or []),
            )
        index_tool_args = _repo_tool_base_args(repo_path)
        context_tool_args = _repo_context_tool_args(repo_path, context_hints)
        index_status, index_error = _maybe_refresh_repo_index(cfg, repo_path, tool_ids, index_tool_args)
        result = execute_engine_tool(
            cfg,
            "repo.context_bundle",
            {
                **context_tool_args,
                "task": context_hints["task"],
                "budget_chars": budget_chars,
                "required_files": context_hints["required_files"],
                "limit": limit,
            },
        )
        payload = _structured_payload(result)
        if not isinstance(payload, dict):
            raise RuntimeError("repo.context_bundle did not return a structured object")
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        bundle_has_evidence = _context_bundle_has_evidence(payload)
        empty_reason = "repo.context_bundle returned no actionable repo evidence"
        text = _format_context_bundle(payload, metadata)
        if not bundle_has_evidence:
            text = _fallback_text(fallback, empty_reason)
        written_artifact = _write_artifact(
            artifact_path,
            {
                "source": "repo.context_bundle",
                "index_source": metadata.get("index_source"),
                "repo_path": str(repo_path),
                "engine_workspace_root": context_tool_args.get("__workspace_root"),
                "path_scope": context_tool_args.get("path_scope"),
                "graph_hints": context_hints,
                "task_id": task.get("task_id"),
                "fallback_used": not bundle_has_evidence,
                "fallback_reason": empty_reason if not bundle_has_evidence else None,
                "tool_result": result,
                "bundle": payload,
            },
        )
        return RepoContextResult(
            text=text,
            source="repo.context_bundle",
            fallback_used=not bundle_has_evidence,
            error=empty_reason if not bundle_has_evidence else None,
            artifact_path=str(written_artifact) if written_artifact else None,
            path_scope=str(context_tool_args.get("path_scope") or "."),
            required_files=list(context_hints.get("required_files") or []),
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
            path_scope=str(context_hints.get("path_scope") or "."),
            required_files=list(context_hints.get("required_files") or []),
        )


def _maybe_refresh_repo_index(
    cfg: ResolvedConfig,
    repo_path: Path,
    tool_ids: set[str],
    repo_tool_args: dict[str, Any],
) -> tuple[str | None, str | None]:
    if "repo.index" not in tool_ids:
        return "skipped_tool_unavailable", None
    if not _repo_index_path_is_ignored(repo_path):
        return "skipped_unignored_store_path", ".tandem/repo-index.json is not ignored by this repo"
    try:
        execute_engine_tool(cfg, "repo.index", dict(repo_tool_args))
        return "refreshed", None
    except Exception as exc:
        return "refresh_failed", str(exc)


def _repo_tool_base_args(repo_path: Path) -> dict[str, Any]:
    """Scope Tandem repo-intelligence tools to the resolved repo workspace.

    The engine's repo graph tools enforce a workspace governance envelope. When
    ACA runs in Docker, the repo path visible to Python may differ from the host
    path visible to the engine, so pass the engine-visible checkout as the tool
    workspace and query the repository as "." within that workspace.
    """
    return {
        "__workspace_root": str(engine_visible_path(repo_path)),
        "repo_path": ".",
        "path_scope": ".",
        "readable_paths": ["."],
    }


def _repo_context_tool_args(repo_path: Path, context_hints: dict[str, Any]) -> dict[str, Any]:
    args = _repo_tool_base_args(repo_path)
    args["path_scope"] = str(context_hints.get("path_scope") or ".")
    return args


def repo_context_hints_for_task(task: dict[str, Any] | None) -> dict[str, Any]:
    """Return deterministic graph-routing hints for ACA planning.

    These hints are intentionally safe to expose in run status: they contain no
    tool output or secrets, only task-derived search text and relative paths.
    """
    task = task or {}
    return {
        "task": _task_query_text(task),
        "path_scope": _task_path_scope(task),
        "required_files": _task_required_files(task),
    }


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
    for key in ("task_id", "identifier", "title", "description"):
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
    labels = [str(value).strip() for value in task.get("labels") or [] if str(value).strip()]
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")
    domain_hints = _task_domain_hints(task)
    if domain_hints:
        parts.append("Routing hints: " + ", ".join(domain_hints))
    target_files = _task_target_files(task)
    if target_files:
        parts.append(f"Target files: {', '.join(target_files[:20])}")
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


def _task_required_files(task: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for value in [*_task_target_files(task), *_task_domain_required_files(task)]:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        files.append(text)
    return files


def _task_path_scope(task: dict[str, Any]) -> str:
    target_scope = _scope_from_target_files(_task_target_files(task))
    if target_scope:
        return target_scope
    text = _task_query_text_without_targets(task)
    lowered_text = text.lower()
    if re.search(r"\bmh-\d+\b|\bmeta[- ]harness\b|tandem-meta-harness-eval", lowered_text):
        return "crates/tandem-meta-harness-eval"
    if _is_github_projects_coder_task(lowered_text):
        return "crates/tandem-server/src/http"
    for match in re.finditer(r"(?:^|[\s`'\"])((?:crates|packages|apps|src|scripts|docs|tests)/[A-Za-z0-9_./-]+)", text):
        scope = _scope_from_path(match.group(1))
        if scope:
            return scope
    return "."


def _task_domain_hints(task: dict[str, Any]) -> list[str]:
    lowered_text = _task_query_text_without_targets(task).lower()
    if not _is_github_projects_coder_task(lowered_text):
        return []
    return [
        "CoderGithubProjectBinding",
        "CoderGithubProjectInboxResponse",
        "CoderGithubProjectIntakeInput",
        "schema_drift",
        "live_schema_fingerprint",
        *_task_domain_required_files(task),
    ]


def _is_github_projects_coder_task(lowered_text: str) -> bool:
    has_github_project = "github projects" in lowered_text or "github project" in lowered_text
    has_coder_context = "coder" in lowered_text or "intake" in lowered_text
    return has_github_project and has_coder_context


def _task_domain_required_files(task: dict[str, Any]) -> list[str]:
    lowered_text = _task_query_text_without_targets(task).lower()
    if not _is_github_projects_coder_task(lowered_text):
        return []
    return [
        "crates/tandem-server/src/http/coder_parts/part05.rs",
        "crates/tandem-server/src/http/coder_parts/part09.rs",
        "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
    ]


def _context_bundle_has_evidence(bundle: dict[str, Any]) -> bool:
    evidence_keys = (
        "suggested_first_reads",
        "likely_files",
        "relevant_symbols",
        "graph_edges",
        "test_targets",
    )
    for key in evidence_keys:
        values = bundle.get(key)
        if isinstance(values, list) and any(bool(value) for value in values):
            return True
    return False


def _task_query_text_without_targets(task: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("task_id", "identifier", "title", "description"):
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
    return "\n".join(parts)


def _scope_from_target_files(files: list[str]) -> str | None:
    scopes = [_scope_from_path(path) for path in files]
    scopes = [scope for scope in scopes if scope]
    if not scopes:
        return None
    if len(scopes) == 1:
        return scopes[0]
    split_scopes = [scope.split("/") for scope in scopes]
    common: list[str] = []
    for parts in zip(*split_scopes):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common) if common else None


def _scope_from_path(path: str) -> str | None:
    cleaned = str(path or "").strip().strip("`'\".,:;)")
    cleaned = cleaned.replace("\\", "/").lstrip("./")
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        return None
    parts = [part for part in cleaned.split("/") if part]
    if not parts:
        return None
    if parts[:2] == ["docs", "internal"]:
        return None
    if parts[0] in {"crates", "packages", "apps"} and len(parts) >= 2:
        return "/".join(parts[:2])
    if "." in parts[-1] and len(parts) > 1:
        return "/".join(parts[:-1])
    if len(parts) > 1:
        return "/".join(parts)
    return None


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
    _append_path_list(lines, "Required edit files", metadata.get("required_files"))
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
        "Use Required edit files as the preferred worker deliverables. Treat Suggested first reads, "
        "Likely files, Relevant symbols, and Graph evidence as discovery/read-only context unless a "
        "required edit file is missing or proves unrelated after inspection."
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
