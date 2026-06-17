"""phases/planning.py -- Manager prompt, subtask decomposition, and pre-satisfaction.

This module owns the manager (planning) prompt phase:
1. Build and run the Tandem manager prompt
2. Parse the JSON plan (or fall back to a plain-text plan)
3. Decompose the plan into subtasks via ``derive_subtasks``
4. Pre-screen subtasks against the repository to discover already-satisfied ones
5. Detect the "no-targets" early-exit condition
6. Prepare the pending-subtask list for worker dispatch

The planning phase repeats inside the repair loop (max_loops iterations).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import logging
import re
from pathlib import Path
from typing import Any

from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.task_contract import task_contract_payload

logger = logging.getLogger("aca.phases.planning")

_MANAGER_PLAN_REQUIRED_KEYS = {"summary", "subtasks", "risks", "tests"}
_PRODUCTION_SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".ts",
    ".tsx",
}


def _normalize_repo_relative_path(value: Any) -> str:
    rel_path = str(value or "").strip().replace("\\", "/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if not rel_path or rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../"):
        return ""
    if "/../" in f"/{rel_path}/":
        return ""
    return rel_path


def _subtask_declared_files(subtask: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    for key in ("files", "target_files"):
        raw_values = subtask.get(key)
        if not isinstance(raw_values, list):
            continue
        for raw_path in raw_values:
            rel_path = _normalize_repo_relative_path(raw_path)
            if rel_path:
                files.add(rel_path)
    return files


def _partial_diff_changed_files(artifact: dict[str, Any]) -> list[str]:
    raw_files = artifact.get("changed_files")
    if not isinstance(raw_files, list):
        return []
    return list(
        dict.fromkeys(
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in raw_files)
            if rel_path and rel_path != "__aca_temp_probe.txt"
        )
    )


def _compact_partial_diff_repair_context(worker_output_excerpt: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in str(worker_output_excerpt or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if "aca preserved this partial worker diff" in lowered:
            continue
        if "partial diff is not treated as a completed worker result" in lowered:
            continue
        if line == "- __aca_temp_probe.txt":
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    context = " ".join(lines).strip()
    if not context and "engine_prompt_timeout" in str(worker_output_excerpt or "").lower():
        context = "ENGINE_PROMPT_TIMEOUT before the worker returned a terminal response."
    return context[:500].rstrip()


def _repo_path_looks_like_test_file(path: str) -> bool:
    rel_path = _normalize_repo_relative_path(path)
    if not rel_path:
        return False
    name = Path(rel_path).name.lower()
    return (
        "/tests/" in f"/{rel_path.lower()}/"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
    )


def _repo_path_looks_like_production_source_file(path: str) -> bool:
    rel_path = _normalize_repo_relative_path(path)
    if not rel_path or _repo_path_looks_like_test_file(rel_path):
        return False
    return Path(rel_path).suffix.lower() in _PRODUCTION_SOURCE_EXTENSIONS


def _all_changed_files_are_tests(changed_files: list[str]) -> bool:
    return bool(changed_files) and all(_repo_path_looks_like_test_file(path) for path in changed_files)


def _changed_files_include_source_and_test(changed_files: list[str]) -> bool:
    normalized = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in changed_files)
        if rel_path
    ]
    return bool(normalized) and any(_repo_path_looks_like_test_file(path) for path in normalized) and any(
        not _repo_path_looks_like_test_file(path) for path in normalized
    )


def _partial_diff_retry_should_verify_first(subtask: dict[str, Any], changed_files: list[str]) -> bool:
    if not _changed_files_include_source_and_test(changed_files):
        return False
    excerpt = str(subtask.get("repair_worker_output_excerpt") or "").lower()
    return (
        "worker_incomplete_verifiable_diff" in excerpt
        or "source plus required-test partial diff" in excerpt
        or "verify/fix the preserved patch" in excerpt
    )


def _paired_production_path_for_test_file(test_path: str, candidate_files: list[str]) -> str:
    test_rel = _normalize_repo_relative_path(test_path)
    if not test_rel:
        return ""
    candidate_set = set(candidate_files)
    test_obj = Path(test_rel)
    name = test_obj.name
    direct_candidates: list[str] = []
    if name.endswith("_test.py"):
        direct_candidates.append(test_obj.with_name(f"{name.removesuffix('_test.py')}.py").as_posix())
    for test_suffix, source_suffix in (
        (".test.py", ".py"),
        (".test.ts", ".ts"),
        (".test.tsx", ".tsx"),
        (".spec.ts", ".ts"),
        (".spec.tsx", ".tsx"),
    ):
        if name.endswith(test_suffix):
            direct_candidates.append(test_obj.with_name(f"{name.removesuffix(test_suffix)}{source_suffix}").as_posix())
    if name.startswith("test_") and name.endswith(".py"):
        direct_candidates.append(test_obj.with_name(f"{name.removeprefix('test_')}").as_posix())
        parent = test_obj.parent
        if parent.name == "tests":
            direct_candidates.append(parent.parent.joinpath(name.removeprefix("test_")).as_posix())
    for candidate in direct_candidates:
        if candidate in candidate_set and _repo_path_looks_like_production_source_file(candidate):
            return candidate

    test_stem = test_obj.stem
    for suffix in (".test", ".spec", "_test"):
        test_stem = test_stem.removesuffix(suffix)
    test_stem = test_stem.removeprefix("test_")
    scored_candidates: list[tuple[int, str]] = []
    for candidate in candidate_files:
        if not _repo_path_looks_like_production_source_file(candidate):
            continue
        candidate_obj = Path(candidate)
        candidate_stem = candidate_obj.stem
        if test_stem != candidate_stem and not test_stem.endswith(f"_{candidate_stem}"):
            continue
        same_parent_score = 0 if candidate_obj.parent == test_obj.parent else 1
        scored_candidates.append((same_parent_score, candidate))
    if not scored_candidates:
        return ""
    return sorted(scored_candidates)[0][1]


def _direct_production_candidates_for_test_file(test_path: str) -> list[str]:
    test_rel = _normalize_repo_relative_path(test_path)
    if not test_rel:
        return []
    test_obj = Path(test_rel)
    name = test_obj.name
    candidates: list[str] = []
    if name.endswith("_test.py"):
        candidates.append(test_obj.with_name(f"{name.removesuffix('_test.py')}.py").as_posix())
    for test_suffix, source_suffix in (
        (".test.py", ".py"),
        (".test.ts", ".ts"),
        (".test.tsx", ".tsx"),
        (".spec.ts", ".ts"),
        (".spec.tsx", ".tsx"),
    ):
        if name.endswith(test_suffix):
            candidates.append(test_obj.with_name(f"{name.removesuffix(test_suffix)}{source_suffix}").as_posix())
    if name.startswith("test_") and name.endswith(".py"):
        candidates.append(test_obj.with_name(f"{name.removeprefix('test_')}").as_posix())
        if test_obj.parent.name == "tests":
            candidates.append(test_obj.parent.parent.joinpath(name.removeprefix("test_")).as_posix())
    return [
        rel_path
        for rel_path in dict.fromkeys(_normalize_repo_relative_path(candidate) for candidate in candidates)
        if rel_path and _repo_path_looks_like_production_source_file(rel_path)
    ]


def _source_partial_declared_test_followup_files(subtask: dict[str, Any]) -> list[str]:
    changed_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_changed_files") or [])
        )
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    if not changed_files or any(_repo_path_looks_like_test_file(path) for path in changed_files):
        return []
    declared_tests = [
        path
        for path in sorted(_subtask_declared_files(subtask))
        if _repo_path_looks_like_test_file(path)
    ]
    if not declared_tests:
        return []
    text = "\n".join(
        str(part or "")
        for part in [
            subtask.get("title"),
            subtask.get("goal"),
            subtask.get("scope_note"),
            *list(subtask.get("acceptance_criteria") or []),
        ]
    ).lower()
    if not any(word in text for word in ("test", "tests", "coverage", "regression")):
        return []
    return declared_tests


def _testless_partial_required_test_files(subtask: dict[str, Any]) -> list[str]:
    excerpt = str(subtask.get("repair_worker_output_excerpt") or "")
    lowered = excerpt.lower()
    marker = "required test files were"
    if marker not in lowered:
        return []
    tail = excerpt[lowered.index(marker) + len(marker) :]
    tail = tail.split("\n", 1)[0].strip(" :.")
    files: list[str] = []
    for raw_path in re.split(r"[,;]\s*|\s+and\s+", tail):
        rel_path = _normalize_repo_relative_path(raw_path.strip(" `'.\t"))
        if rel_path and _repo_path_looks_like_test_file(rel_path):
            files.append(rel_path)
    return list(dict.fromkeys(files))


def _test_only_partial_production_followup_files(
    subtask: dict[str, Any],
    extra_candidate_files: list[str] | None = None,
) -> list[str]:
    changed_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_changed_files") or [])
        )
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    if not _all_changed_files_are_tests(changed_files):
        return []
    declared_candidate_files = list(sorted(_subtask_declared_files(subtask)))
    candidate_files = list(declared_candidate_files)
    for raw_path in extra_candidate_files or []:
        rel_path = _normalize_repo_relative_path(raw_path)
        if rel_path and rel_path not in candidate_files:
            candidate_files.append(rel_path)
    paired_files = [
        paired_path
        for paired_path in (
            _paired_production_path_for_test_file(changed_file, candidate_files)
            for changed_file in changed_files
        )
        if paired_path
    ]
    if paired_files:
        return list(dict.fromkeys(paired_files))

    declared_production_files = [
        path
        for path in declared_candidate_files
        if _repo_path_looks_like_production_source_file(path)
    ]
    if declared_production_files:
        return declared_production_files[:1]
    sticky_production_files = [path for path in candidate_files if _repo_path_looks_like_production_source_file(path)]
    return sticky_production_files[:1]


def _test_only_partial_existing_sibling_production_files(
    ctx: RunContext,
    test_files: list[str],
) -> list[str]:
    try:
        repo_path = ctx.repo_path
    except Exception:
        repo_path = Path(".")
    production_files: list[str] = []
    for test_file in test_files:
        for candidate in _direct_production_candidates_for_test_file(test_file):
            try:
                if (repo_path / candidate).is_file():
                    production_files.append(candidate)
                    break
            except OSError:
                continue
    return list(dict.fromkeys(production_files))



def _select_partial_diff_subtask(
    artifact: dict[str, Any],
    subtasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not subtasks:
        return None
    target_subtask_id = str(artifact.get("subtask_id") or "").strip()
    if target_subtask_id:
        for subtask in subtasks:
            if str(subtask.get("id") or "").strip() == target_subtask_id:
                return subtask
    changed_files = set(_partial_diff_changed_files(artifact))
    if changed_files:
        best_subtask: dict[str, Any] | None = None
        best_overlap = 0
        for subtask in subtasks:
            overlap = len(changed_files.intersection(_subtask_declared_files(subtask)))
            if overlap > best_overlap:
                best_subtask = subtask
                best_overlap = overlap
        if best_subtask is not None:
            return best_subtask
    return subtasks[0]


def _append_unique_repo_paths(subtask: dict[str, Any], paths: list[str]) -> None:
    if not paths:
        return
    for key in ("files", "target_files"):
        values = [
            rel_path
            for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get(key) or []))
            if rel_path
        ]
        for rel_path in paths:
            if rel_path not in values:
                values.append(rel_path)
        subtask[key] = values


def _repo_uses_sibling_python_tests(repo_path: Path) -> bool:
    if (repo_path / "tests").is_dir():
        return False
    try:
        return any((repo_path / "src").glob("**/*_test.py"))
    except Exception:
        return False


def _sibling_python_test_path_for_top_level_test(path: str, declared_files: set[str]) -> str:
    normalized = _normalize_repo_relative_path(path)
    if not normalized.startswith("tests/test_") or not normalized.endswith(".py"):
        return normalized
    test_stem = Path(normalized).stem
    if not test_stem.startswith("test_"):
        return normalized
    source_stem = test_stem.removeprefix("test_")
    source_paths = [
        Path(declared)
        for declared in sorted(declared_files)
        if declared.startswith("src/")
        and Path(declared).suffix == ".py"
        and not Path(declared).stem.endswith("_test")
        and Path(declared).name != "__init__.py"
    ]
    for source_path in source_paths:
        if source_path.stem == source_stem or source_stem.endswith(f"_{source_path.stem}"):
            return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    non_main_sources = [source_path for source_path in source_paths if source_path.stem != "main"]
    if len(non_main_sources) == 1:
        source_path = non_main_sources[0]
        return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    if len(source_paths) == 1:
        source_path = source_paths[0]
        return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    for declared in sorted(declared_files):
        source_path = Path(declared)
        if (
            declared.startswith("src/")
            and source_path.suffix == ".py"
            and source_path.stem == source_stem
        ):
            return source_path.with_name(f"{source_path.stem}_test.py").as_posix()
    return normalized


def _align_python_test_targets_to_repo_conventions(repo_path: Path, subtasks: list[dict[str, Any]]) -> None:
    if not _repo_uses_sibling_python_tests(repo_path):
        return
    for subtask in subtasks:
        declared_files = _subtask_declared_files(subtask)
        if not declared_files:
            continue
        rewrites: dict[str, str] = {}
        for rel_path in declared_files:
            rewritten = _sibling_python_test_path_for_top_level_test(rel_path, declared_files)
            if rewritten and rewritten != rel_path:
                rewrites[rel_path] = rewritten
        if not rewrites:
            continue
        for key in ("files", "target_files"):
            values: list[str] = []
            for raw_path in subtask.get(key) or []:
                rel_path = _normalize_repo_relative_path(raw_path)
                if not rel_path:
                    continue
                values.append(rewrites.get(rel_path, rel_path))
            subtask[key] = list(dict.fromkeys(values))
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        rewrite_note = (
            "ACA aligned generated Python test targets to this repository's sibling "
            "*_test.py convention: "
            + ", ".join(f"{old} -> {new}" for old, new in sorted(rewrites.items()))
            + "."
        )
        if rewrite_note not in existing_scope_note:
            subtask["scope_note"] = f"{existing_scope_note}\n{rewrite_note}".strip()


def _split_dense_serial_subtasks(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
    *,
    max_acceptance_criteria: int = 3,
) -> None:
    """Break oversized subtasks into sequential slices when ACA is dispatching serially."""
    swarm_cfg = getattr(getattr(ctx, "cfg", None), "swarm", None)
    if bool(getattr(swarm_cfg, "enabled", False)):
        return
    if max_acceptance_criteria < 1 or not subtasks:
        return

    split: list[dict[str, Any]] = []
    changed = False
    for subtask in subtasks:
        criteria = [
            str(entry).strip()
            for entry in (subtask.get("acceptance_criteria") or [])
            if str(entry).strip()
        ]
        skip_repair_split = bool(
            subtask.get("carry_forward_patch")
            or subtask.get("carry_forward_patches")
            or subtask.get("discarded_partial_diff_patch")
            or subtask.get("repair_parent_target_files")
        )
        if skip_repair_split or len(criteria) <= max_acceptance_criteria:
            split.append(subtask)
            continue

        chunks = [
            criteria[index : index + max_acceptance_criteria]
            for index in range(0, len(criteria), max_acceptance_criteria)
        ]
        if len(chunks) <= 1:
            split.append(subtask)
            continue

        changed = True
        base_id = str(subtask.get("id") or f"subtask-{len(split) + 1}").strip()
        base_title = str(subtask.get("title") or "Subtask").strip()
        original_scope_note = str(subtask.get("scope_note") or "").strip()
        split_note = (
            "ACA split this dense manager subtask into serial slices because swarm is disabled. "
            "Only one worker slice runs at a time; focus on this slice's acceptance criteria, "
            "then later slices cover the remaining criteria against the same target files."
        )
        for chunk_index, chunk in enumerate(chunks, start=1):
            cloned = dict(subtask)
            cloned["id"] = f"{base_id}-part-{chunk_index}"
            cloned["title"] = f"{base_title} (part {chunk_index}/{len(chunks)})"
            cloned["acceptance_criteria"] = list(chunk)
            cloned["dense_parent_subtask_id"] = base_id
            cloned["dense_part_index"] = chunk_index
            cloned["dense_part_count"] = len(chunks)
            cloned["scope_note"] = (
                f"{original_scope_note}\n{split_note}".strip()
                if split_note not in original_scope_note
                else original_scope_note
            )
            split.append(cloned)

    if changed:
        subtasks[:] = split


def _remote_code_task_requires_worker_execution(task: dict[str, Any]) -> bool:
    """Remote code tasks need an explicit worker verdict, not file-presence proof."""
    source = task.get("source") if isinstance(task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str(task.get("execution_kind") or "").strip()
    return execution_kind == "code_edit" and source_type in {"linear", "github_project"}


def _apply_repo_context_required_files_to_task(task: dict[str, Any], required_files: list[str] | None) -> bool:
    """Promote graph-required files into the task target contract when absent."""
    if not isinstance(task, dict):
        return False
    existing_contract = task_contract_payload(task)
    existing_targets = [
        str(entry).strip()
        for entry in (existing_contract.get("target_files") or task.get("target_files") or [])
        if str(entry).strip()
    ]
    if existing_targets:
        return False
    normalized = list(
        dict.fromkeys(
            str(entry).strip().replace("\\", "/")
            for entry in (required_files or [])
            if str(entry).strip()
        )
    )
    if not normalized:
        return False
    task["target_files"] = normalized
    task.setdefault("task_contract", {})
    if isinstance(task["task_contract"], dict):
        task["task_contract"].setdefault("target_files", normalized)
    return True


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from model output (imported from runner_core)."""
    # Import here to avoid circular imports; runner_core is the owner of this util
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    return _rc._extract_json(text)


def _manager_plan_from_stdout(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate the manager JSON contract."""
    payload = _extract_json(stdout)
    if not isinstance(payload, dict):
        return None, "Manager planning did not return a valid JSON object."
    missing = sorted(_MANAGER_PLAN_REQUIRED_KEYS.difference(payload))
    if missing:
        return None, "Manager planning JSON is missing required key(s): " + ", ".join(missing)
    if not isinstance(payload.get("subtasks"), list):
        return None, "Manager planning JSON field `subtasks` must be a list."
    for key in ("risks", "tests"):
        if not isinstance(payload.get(key), list):
            return None, f"Manager planning JSON field `{key}` must be a list."
    return payload, None


def _prepare_subtasks(ctx: RunContext) -> tuple[list[str], list[dict[str, Any]]]:
    """Call the private runner_core subtask-preparation helper.

    Returns (discovered_files, subtasks).  Kept as a thin bridge so callers
    don't need to import the private helper directly.
    """
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415
    from pathlib import Path
    # Dispatch concurrency is limited later. When swarm is disabled, preserve a
    # small serial queue of manager slices instead of merging all work into one
    # oversized prompt.
    planning_subtask_limit = (
        max(1, int(ctx.cfg.swarm.max_workers or 1))
        if ctx.cfg.swarm.enabled
        else _serial_subtask_limit(ctx.cfg)
    )
    discovered_files, subtasks = _rc._prepare_subtasks_with_discovery(
        ctx.task,
        ctx.manager_plan,
        Path(ctx.repo.get("path") or "."),
        planning_subtask_limit,
        merge_manager_subtasks=bool(ctx.cfg.swarm.enabled),
    )
    return _constrain_invalid_manager_fallback(ctx, discovered_files, subtasks)


def _append_deferred_repair_subtasks(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else None
    if not isinstance(repair, dict):
        repair = ctx.status.get("repair") if isinstance(ctx.status, dict) else None
    if not isinstance(repair, dict):
        return
    raw_deferred = repair.get("deferred_subtasks")
    if not isinstance(raw_deferred, list) or not raw_deferred:
        return
    existing_ids = {str(subtask.get("id") or "").strip() for subtask in subtasks}
    appended = 0
    for item in raw_deferred:
        if not isinstance(item, dict):
            continue
        subtask = dict(item)
        subtask_id = str(subtask.get("id") or "").strip()
        if not subtask_id or subtask_id in existing_ids:
            continue
        scope_note = str(subtask.get("scope_note") or "").strip()
        continuation_note = (
            "ACA deferred this serial subtask while repairing an earlier failed slice; "
            "resume it after the repair slice completes."
        )
        if continuation_note not in scope_note:
            subtask["scope_note"] = f"{scope_note}\n{continuation_note}".strip()
        subtasks.append(subtask)
        existing_ids.add(subtask_id)
        appended += 1
    if appended:
        repair["deferred_subtasks_appended"] = appended


def _serial_subtask_limit(cfg: Any) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_SERIAL_SUBTASK_LIMIT", "") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_SERIAL_SUBTASK_LIMIT=%r", raw)
    return 4


def _safe_repo_context_path(rel_path: Any, repo_path: Path) -> str:
    rel_text = str(rel_path or "").strip()
    if not rel_text or rel_text.startswith("/"):
        return ""
    rel = Path(rel_text)
    if ".." in rel.parts:
        return ""
    if not (repo_path / rel).is_file():
        return ""
    return rel.as_posix()


def _repo_context_fallback_files(ctx: RunContext) -> list[str]:
    repo_path = Path(ctx.repo.get("path") or ctx.repo_path or ".")
    repo_context = ctx.blackboard.get("repo_context") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repo_context, dict):
        return []
    candidates: list[str] = []
    for rel_path in repo_context.get("required_files") or []:
        candidates.append(str(rel_path or ""))
    artifact_path = str(repo_context.get("artifact_path") or "").strip()
    if artifact_path:
        try:
            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except Exception:
            artifact = {}
        bundle = artifact.get("bundle") if isinstance(artifact, dict) else {}
        if isinstance(bundle, dict):
            for rel_path in bundle.get("suggested_first_reads") or []:
                candidates.append(str(rel_path or ""))
            for item in bundle.get("likely_files") or []:
                if isinstance(item, dict):
                    candidates.append(str(item.get("file_path") or ""))
            for rel_path in bundle.get("test_targets") or []:
                candidates.append(str(rel_path or ""))
    fallback_files: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        rel_path = _safe_repo_context_path(candidate, repo_path)
        if rel_path and rel_path not in seen:
            fallback_files.append(rel_path)
            seen.add(rel_path)
    return fallback_files


def _criteria_matching(acceptance_criteria: list[str], keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for criterion in acceptance_criteria:
        lowered = criterion.lower()
        if any(keyword in lowered for keyword in keywords):
            matches.append(criterion)
    return matches


def _all_task_acceptance_criteria(ctx: RunContext) -> list[str]:
    criteria = task_contract_payload(ctx.task).get("acceptance_criteria") or ctx.task.get("acceptance_criteria") or []
    return [str(entry).strip() for entry in criteria if str(entry).strip()]


def _deterministic_invalid_manager_subtasks(
    ctx: RunContext,
    fallback_files: list[str],
    existing_subtasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a stable fallback plan from repo-context files after manager JSON failure."""
    if not fallback_files:
        return []

    max_workers = max(1, int(getattr(getattr(ctx.cfg, "swarm", None), "max_workers", 1) or 1))
    title = str(ctx.task.get("title") or ctx.task.get("local_goal") or "ACA fallback plan").strip()
    acceptance_criteria = _all_task_acceptance_criteria(ctx)
    by_name = {Path(path).name: path for path in fallback_files}

    planned: list[dict[str, Any]] = []

    repository_files = [
        path
        for path in (by_name.get("repository.py"), by_name.get("repository_test.py"))
        if path
    ]
    if repository_files:
        planned.append(
            {
                "id": "fallback-repository-isolation",
                "title": f"{title} - repository isolation",
                "goal": "Implement and verify per-issue repository worktree, branch, and pinned-base planning.",
                "files": repository_files,
                "target_files": repository_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("worktree", "branch", "base revision", "mutable working directory", "parallel"),
                )
                or acceptance_criteria,
            }
        )

    intake_files = [path for path in (by_name.get("task_intake.py"),) if path]
    if intake_files:
        planned.append(
            {
                "id": "fallback-intake-conflicts",
                "title": f"{title} - intake conflict tracking",
                "goal": "Record claimed-issue isolation metadata and detect overlapping active run edits before merge risk.",
                "files": intake_files,
                "target_files": intake_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("claimed", "touched", "generated artifact", "overlap", "conflict", "pause", "serialize", "operator"),
                )
                or acceptance_criteria,
            }
        )

    finalize_files = [
        path
        for path in (by_name.get("finalize.py"), by_name.get("pr_body.py"))
        if path
    ]
    if finalize_files:
        planned.append(
            {
                "id": "fallback-finalize-pr-metadata",
                "title": f"{title} - finalize and PR metadata",
                "goal": "Preserve cleanup behavior and include Linear issue plus ACA run metadata in PR output.",
                "files": finalize_files,
                "target_files": finalize_files,
                "acceptance_criteria": _criteria_matching(
                    acceptance_criteria,
                    ("cleanup", "success", "failure", "lease", "pr metadata", "linear", "run id"),
                )
                or acceptance_criteria,
            }
        )

    covered = {path for subtask in planned for path in subtask.get("files", [])}
    remaining = [path for path in fallback_files if path not in covered]
    if remaining:
        planned.append(
            {
                "id": "fallback-remaining-repo-context",
                "title": f"{title} - remaining repo-context targets",
                "goal": "Finish the remaining repo-context target files needed by the task contract.",
                "files": remaining,
                "target_files": remaining,
                "acceptance_criteria": acceptance_criteria,
            }
        )

    if not planned and existing_subtasks:
        planned = [dict(existing_subtasks[0])]
        planned[0]["files"] = list(fallback_files)
        planned[0]["target_files"] = list(fallback_files)
        planned[0]["acceptance_criteria"] = planned[0].get("acceptance_criteria") or acceptance_criteria

    fallback_note = (
        "ACA replaced invalid manager JSON with deterministic repo-context fallback targets: "
        + ", ".join(fallback_files)
        + "."
    )
    for index, subtask in enumerate(planned, start=1):
        subtask.setdefault("id", f"fallback-{index}")
        subtask.setdefault("title", f"{title} - fallback slice {index}")
        subtask.setdefault("goal", title)
        subtask.setdefault("acceptance_criteria", acceptance_criteria)
        subtask.setdefault("files", list(fallback_files))
        subtask.setdefault("target_files", list(subtask.get("files") or fallback_files))
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        if fallback_note not in existing_scope_note:
            subtask["scope_note"] = f"{existing_scope_note}\n{fallback_note}".strip()

    return planned[:max_workers]


def _constrain_invalid_manager_fallback(
    ctx: RunContext,
    discovered_files: list[str],
    subtasks: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    if not getattr(ctx, "_manager_fallback_required", False):
        return discovered_files, subtasks
    if "manager_invalid_plan" not in ctx.blackboard:
        return discovered_files, subtasks
    fallback_files = _repo_context_fallback_files(ctx)
    if not fallback_files:
        return [], []
    fallback_subtasks = _deterministic_invalid_manager_subtasks(ctx, fallback_files, subtasks)
    return fallback_files, fallback_subtasks


def _carry_forward_partial_diff_artifacts(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    artifacts = repair.get("partial_diff_artifacts") if isinstance(repair, dict) else []
    if not isinstance(artifacts, list) or not artifacts:
        return
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        patch_path = str(artifact.get("patch_path") or "").strip()
        if not patch_path:
            continue
        subtask = _select_partial_diff_subtask(artifact, subtasks)
        if subtask is None or subtask.get("carry_forward_patch") or subtask.get("discarded_partial_diff_patch"):
            continue
        changed_files = _partial_diff_changed_files(artifact)
        worker_output_excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        should_reapply_patch = _partial_diff_patch_is_reusable(worker_output_excerpt)
        excerpt_limit = 1200 if should_reapply_patch else 360
        if len(worker_output_excerpt) > excerpt_limit:
            worker_output_excerpt = worker_output_excerpt[:excerpt_limit].rstrip() + "\n..."
        rejected_failure_summary = (
            _partial_diff_rejected_failure_summary(worker_output_excerpt)
            if not should_reapply_patch
            else ""
        )
        if should_reapply_patch:
            _append_unique_repo_paths(subtask, changed_files)
            subtask["carry_forward_patch"] = patch_path
        else:
            subtask["discarded_partial_diff_patch"] = patch_path
            if rejected_failure_summary:
                subtask["repair_failure_summary"] = rejected_failure_summary
            task_obj = getattr(ctx, "task", None)
            parent_contract = task_contract_payload(task_obj) if isinstance(task_obj, dict) else {}
            parent_target_files = [
                rel_path
                for rel_path in (
                    _normalize_repo_relative_path(raw_path)
                    for raw_path in (
                        parent_contract.get("target_files")
                        or task_obj.get("target_files")
                        or []
                    )
                )
                if rel_path
            ] if isinstance(task_obj, dict) else []
            if parent_target_files:
                _append_unique_repo_paths(subtask, parent_target_files)
                subtask["repair_parent_target_files"] = parent_target_files
                criteria = [
                    str(entry).strip()
                    for entry in (subtask.get("acceptance_criteria") or [])
                    if str(entry).strip()
                ]
                rewritten: list[str] = []
                replacement = (
                    "Keep repair edits scoped to the parent task target files: "
                    + ", ".join(parent_target_files)
                    + "."
                )
                for entry in criteria:
                    lowered = entry.lower()
                    if "do not expand" in lowered and "beyond" in lowered:
                        if replacement not in rewritten:
                            rewritten.append(replacement)
                        continue
                    rewritten.append(entry)
                if replacement not in rewritten:
                    rewritten.append(replacement)
                subtask["acceptance_criteria"] = rewritten
        subtask["repair_source_subtask_id"] = str(artifact.get("subtask_id") or "").strip()
        subtask["repair_source_worker_id"] = str(artifact.get("worker_id") or "").strip()
        subtask["repair_changed_files"] = changed_files
        if worker_output_excerpt:
            subtask["repair_worker_output_excerpt"] = worker_output_excerpt
            criteria = [str(entry).strip() for entry in (subtask.get("acceptance_criteria") or []) if str(entry).strip()]
            if should_reapply_patch:
                repair_criterion = (
                    "Resolve the recovered partial-diff blocker before expanding scope: "
                    + worker_output_excerpt.replace("\n", " ")[:500]
                )
            else:
                repair_criterion = (
                    "Replace the rejected or incomplete partial-diff approach before expanding scope; do not carry "
                    "forward helper-only, unverified, self-referential, or local-oracle coverage."
                )
            if repair_criterion not in criteria:
                subtask["acceptance_criteria"] = [repair_criterion, *criteria]
        existing_scope_note = str(subtask.get("scope_note") or "").strip()
        repair_context = _compact_partial_diff_repair_context(worker_output_excerpt)
        changed_file_note = (
            " The saved diff touched these files; read and finish them before adding new scope: "
            + ", ".join(changed_files)
            + "."
            if changed_files
            else ""
        )
        blocker_note = (
            "\nRecovered partial-diff blocker/context:\n" + repair_context
            if repair_context and should_reapply_patch
            else ""
        )
        if should_reapply_patch:
            carry_note = (
                "ACA will apply the preserved partial worker diff before this retry so the worker can continue "
                "from the previous attempt instead of repeating it."
                f"{changed_file_note}{blocker_note}"
            )
        else:
            rejected_changed_file_note = (
                " The rejected diff touched these files: "
                + ", ".join(changed_files)
                + ". Inspect the current target files instead of copying that patch."
                if changed_files
                else ""
            )
            parent_target_note = (
                " Active repair targets are the parent task target files: "
                + ", ".join(subtask.get("repair_parent_target_files") or [])
                + "."
                if subtask.get("repair_parent_target_files")
                else ""
            )
            summary_note = f" Failure summary: {rejected_failure_summary}." if rejected_failure_summary else ""
            carry_note = (
                "ACA rejected the preserved partial worker diff for this retry because the recovered notes describe "
                "incomplete, unverified, helper-only, self-referential, or test-only coverage. Start from the clean "
                "target files, remove that approach if present, and add coverage that calls existing production code "
                "instead."
                f"{rejected_changed_file_note}{parent_target_note}{summary_note}"
            )
        subtask["scope_note"] = f"{existing_scope_note}\n{carry_note}".strip()
        if should_reapply_patch:
            _narrow_carried_partial_diff_subtask(subtask)
        else:
            _compact_rejected_partial_diff_subtask(subtask)


def _partial_diff_rejected_failure_summary(worker_output_excerpt: str) -> str:
    text = worker_output_excerpt.lower()
    reasons: list[str] = []
    if "engine_prompt_timeout" in text:
        reasons.append("the worker timed out before a terminal response")
    if "worker_off_track_testless_diff" in text or (
        "changed only non-test files" in text and "required test files" in text
    ):
        reasons.append("the worker drifted off the required test-first path")
    if "verification not run" in text:
        reasons.append("verification did not run")
    if any(marker in text for marker in ("helper-only", "test-only helper", "local oracle", "self-referential")):
        reasons.append("the diff appeared helper-only or self-referential")
    if any(marker in text for marker in ("unproductive partial diff", "unproductive diff")):
        reasons.append("ACA flagged the diff as unproductive")
    if any(marker in text for marker in ("runaway guard", "diff exceeded aca runaway", "giant patch")):
        reasons.append("ACA flagged the diff as runaway-sized")
    if any(marker in text for marker in ("changes only string wording", "comment-only", "tautological")):
        reasons.append("the diff did not add meaningful regression coverage")
    if "missing production helper" in text:
        reasons.append("the test called a missing production helper")
    if any(marker in text for marker in ("not wired", "does not show", "limited to message formatting")):
        reasons.append("the diff was not wired into the production path")
    if not reasons:
        first_line = next((line.strip() for line in worker_output_excerpt.splitlines() if line.strip()), "")
        if first_line:
            reasons.append(first_line[:160])
    return "; ".join(dict.fromkeys(reasons))[:260]


def _partial_diff_patch_is_reusable(worker_output_excerpt: str) -> bool:
    text = worker_output_excerpt.lower()
    rejected_markers = (
        "self-referential",
        "test-only constant",
        "test-only enum",
        "test-only helper",
        "does not appear to exercise actual",
        "does not exercise actual",
        "only asserts those same",
        "standalone simulation",
        "local oracle",
        "limited to message formatting",
        "only message formatting",
        "not wired into",
        "does not show this readiness error being wired",
        "not covered by the added test",
        "verification not run",
        "unproductive partial diff",
        "unproductive diff",
        "runaway guard",
        "diff exceeded aca runaway",
        "giant patch",
        "changes only string wording",
        "comment-only",
        "tautological",
        "missing production helper",
        "no-op",
        "redundant",
        "worker_off_track_testless_diff",
        "changed only non-test files",
        "changed only required test files",
        "required test files",
        "worker drifted off the required regression/test coverage path",
    )
    return not any(marker in text for marker in rejected_markers)


def _sanitize_partial_diff_artifact_paths_in_plan(plan: dict[str, Any]) -> None:
    subtasks = plan.get("subtasks") if isinstance(plan, dict) else None
    if not isinstance(subtasks, list):
        return
    replacement = (
        "Use ACA's repair directive and failure summary as evidence; do not read absolute "
        "patch paths or replay rejected partial diffs."
    )
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        criteria = subtask.get("acceptance_criteria")
        if not isinstance(criteria, list):
            continue
        sanitized: list[Any] = []
        for entry in criteria:
            text = str(entry)
            lowered = text.lower()
            if (
                "patch" in lowered
                and ("/workspace/" in text or "runs/" in lowered or "artifacts/" in lowered)
            ):
                if replacement not in sanitized:
                    sanitized.append(replacement)
                continue
            sanitized.append(entry)
        subtask["acceptance_criteria"] = sanitized


def _deterministic_testless_partial_diff_repair_plan(ctx: RunContext) -> dict[str, Any] | None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    artifacts = repair.get("partial_diff_artifacts") if isinstance(repair, dict) else []
    if not isinstance(artifacts, list) or not artifacts:
        return None
    parent_contract = task_contract_payload(ctx.task) if isinstance(ctx.task, dict) else {}
    parent_targets = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (parent_contract.get("target_files") or ctx.task.get("target_files") or [])
        )
        if rel_path
    ] if isinstance(ctx.task, dict) else []
    complementary_plan = _deterministic_complementary_partial_diff_repair_plan(ctx, artifacts, parent_targets)
    if complementary_plan is not None:
        return complementary_plan
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        excerpt = str(artifact.get("worker_output_excerpt") or "").strip()
        if not excerpt:
            continue
        lowered = excerpt.lower()
        changed_files = _partial_diff_changed_files(artifact)
        patch_path = str(artifact.get("patch_path") or "").strip()
        failure_summary = _partial_diff_rejected_failure_summary(excerpt)
        if "changed only non-test files" in lowered and "required test files" in lowered:
            parent_target_set = set(parent_targets)
            source_files = [path for path in changed_files if _repo_path_looks_like_production_source_file(path)]
            if parent_target_set:
                source_files = [path for path in source_files if path in parent_target_set]
            required_test_files = _testless_partial_required_test_files({"repair_worker_output_excerpt": excerpt})
            parsed_required_test_files = list(required_test_files)
            if parent_target_set:
                required_test_files = [path for path in required_test_files if path in parent_target_set]
            if not required_test_files:
                required_test_files = [
                    path
                    for path in parent_targets
                    if _repo_path_looks_like_test_file(path)
                ]
            if parent_targets:
                active_files = list(dict.fromkeys([*required_test_files, *parent_targets]))
                source_files = [
                    path
                    for path in active_files
                    if _repo_path_looks_like_production_source_file(path)
                ]
            else:
                active_files = list(dict.fromkeys([*required_test_files, *source_files]))
            deferred_files = [
                path
                for path in list(dict.fromkeys([*parsed_required_test_files, *changed_files]))
                if path not in active_files
            ]
            if not source_files or not required_test_files or not active_files:
                continue
            source_text = ", ".join(source_files)
            test_text = ", ".join(required_test_files)
            subtask = {
                "id": str(artifact.get("subtask_id") or "testless-diff-repair").strip() or "testless-diff-repair",
                "title": "Repair testless partial diff",
                "goal": (
                    "Replace the rejected source-only partial worker diff with a test-backed repair for "
                    + ", ".join(active_files)
                    + "."
                ),
                "files": active_files,
                "target_files": active_files,
                "acceptance_criteria": [
                    "Read and edit the required test file(s) first: " + test_text + ".",
                    "Then make only the minimal production change needed in: " + source_text + ".",
                    "Do not copy or replay the rejected partial patch; use it only as failure evidence.",
                    "Do not mark this repair complete until the diff includes real coverage in "
                    + test_text
                    + " and narrow verification has run or a concrete blocker is recorded.",
                ],
                "discarded_partial_diff_patch": patch_path,
                "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
                "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
                "repair_changed_files": changed_files,
                "repair_requires_test_followup": required_test_files,
                "repair_worker_output_excerpt": excerpt[:1200],
                "repair_failure_summary": failure_summary,
                "repair_parent_target_files": parent_targets,
                "deterministic_testless_repair": True,
                "deterministic_partial_diff_repair": True,
                "write_required": True,
                "scope_note": (
                    "ACA generated this repair plan deterministically after detecting a worker_off_track "
                    "testless diff. Active repair targets are the in-contract required test file(s) plus "
                    "the parent task target file(s): "
                    + ", ".join(active_files)
                    + "."
                ),
            }
            if deferred_files:
                subtask["repair_deferred_files"] = deferred_files
                subtask["scope_note"] += (
                    " ACA deferred out-of-contract partial-diff files for later manager scope: "
                    + ", ".join(deferred_files)
                    + "."
                )
            return {
                "summary": (
                    "Deterministic repair for a worker_off_track testless partial diff; skipped a second "
                    "manager planning round-trip and narrowed the retry to the changed source plus required tests."
                ),
                "subtasks": [subtask],
                "risks": [
                    "The rejected partial patch is not replayed automatically; the worker must rebuild the repair from a clean checkout."
                ],
                "tests": [
                    "Run the narrowest deterministic verification for " + ", ".join(active_files) + "."
                ],
            }
        if "changed only required test files" not in lowered and "test-only" not in lowered:
            continue
        test_files = [path for path in changed_files if _repo_path_looks_like_test_file(path)]
        production_followup_files = _test_only_partial_production_followup_files(
            {
                "repair_changed_files": changed_files,
                "files": parent_targets,
                "target_files": parent_targets,
            },
            parent_targets,
        )
        if not production_followup_files:
            production_followup_files = _test_only_partial_existing_sibling_production_files(ctx, test_files)
        active_files = list(dict.fromkeys([*production_followup_files, *test_files]))
        if not test_files or not production_followup_files or not active_files:
            continue
        production_text = ", ".join(production_followup_files)
        test_text = ", ".join(test_files)
        subtask = {
            "id": str(artifact.get("subtask_id") or "test-only-diff-repair").strip() or "test-only-diff-repair",
            "title": "Repair test-only partial diff",
            "goal": (
                "Replace the rejected test-only partial worker diff with a production-backed repair for "
                + ", ".join(active_files)
                + "."
            ),
            "files": active_files,
            "target_files": active_files,
            "acceptance_criteria": [
                "Read the changed test file(s): " + test_text + ".",
                "Make the first new repair edit in the required production file(s): " + production_text + ".",
                "Do not copy or replay the rejected partial patch; use it only as failure evidence.",
                "Do not mark this repair complete with a test-only diff; leave a substantive production diff in "
                + production_text
                + " or record a concrete blocker explaining why no production edit is safe.",
            ],
            "discarded_partial_diff_patch": patch_path,
            "repair_source_subtask_id": str(artifact.get("subtask_id") or "").strip(),
            "repair_source_worker_id": str(artifact.get("worker_id") or "").strip(),
            "repair_changed_files": changed_files,
            "repair_requires_production_followup": production_followup_files,
            "repair_worker_output_excerpt": excerpt[:1200],
            "repair_failure_summary": failure_summary,
            "deterministic_partial_diff_repair": True,
            "write_required": True,
            "scope_note": (
                "ACA generated this repair plan deterministically after detecting a worker test-only diff. "
                "Active repair targets are the required production follow-up file(s) plus the changed test file(s): "
                + ", ".join(active_files)
                + "."
            ),
        }
        return {
            "summary": (
                "Deterministic repair for a worker test-only partial diff; skipped a second manager "
                "planning round-trip and narrowed the retry to the changed tests plus required production follow-up."
            ),
            "subtasks": [subtask],
            "risks": [
                "The rejected partial patch is not replayed automatically; the worker must rebuild the repair from a clean checkout."
            ],
            "tests": [
                "Run the narrowest deterministic verification for " + ", ".join(active_files) + "."
            ],
        }
    return None


def _deterministic_complementary_partial_diff_repair_plan(
    ctx: RunContext,
    artifacts: list[Any],
    parent_targets: list[str],
) -> dict[str, Any] | None:
    test_artifacts: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        changed_files = _partial_diff_changed_files(artifact)
        excerpt = str(artifact.get("worker_output_excerpt") or "").lower()
        if _all_changed_files_are_tests(changed_files) and (
            "changed only required test files" in excerpt or "test-only" in excerpt
        ):
            test_artifacts.append(artifact)
        elif changed_files and all(_repo_path_looks_like_production_source_file(path) for path in changed_files) and (
            "changed only non-test files" in excerpt or "worker_off_track_testless_diff" in excerpt
        ):
            source_artifacts.append(artifact)
    if not test_artifacts or not source_artifacts:
        return None
    for test_artifact in test_artifacts:
        test_files = [path for path in _partial_diff_changed_files(test_artifact) if _repo_path_looks_like_test_file(path)]
        for source_artifact in source_artifacts:
            source_files = [
                path
                for path in _partial_diff_changed_files(source_artifact)
                if _repo_path_looks_like_production_source_file(path)
            ]
            paired_source_files = [
                source_file
                for source_file in source_files
                if any(_paired_production_path_for_test_file(test_file, source_files) == source_file for test_file in test_files)
            ] or source_files[:1]
            if parent_targets:
                paired_source_files = [path for path in paired_source_files if path in parent_targets] or paired_source_files
                test_files = [path for path in test_files if path in parent_targets] or test_files
            active_files = list(dict.fromkeys([*paired_source_files, *test_files]))
            patches = [
                str(source_artifact.get("patch_path") or "").strip(),
                str(test_artifact.get("patch_path") or "").strip(),
            ]
            patches = [patch for patch in dict.fromkeys(patches) if patch]
            if not paired_source_files or not test_files or len(patches) < 2:
                continue
            source_text = ", ".join(paired_source_files)
            test_text = ", ".join(test_files)
            return {
                "summary": (
                    "Deterministic repair for complementary source-only and test-only partial diffs; "
                    "ACA will apply both saved patches before asking the worker to verify and fix the combined diff."
                ),
                "subtasks": [
                    {
                        "id": str(test_artifact.get("subtask_id") or "complementary-diff-repair").strip()
                        or "complementary-diff-repair",
                        "title": "Verify complementary source and test partial diffs",
                        "goal": "Verify and minimally fix the combined source+test repair for " + ", ".join(active_files) + ".",
                        "files": active_files,
                        "target_files": active_files,
                        "acceptance_criteria": [
                            "ACA applied the preserved production patch and test patch before this worker starts; inspect the combined diff in "
                            + ", ".join(active_files)
                            + ".",
                            "Run the narrowest deterministic verification for "
                            + test_text
                            + "; if it fails, fix only the paired production/test behavior in "
                            + ", ".join(active_files)
                            + ".",
                            "Do not expand into broader manager scope unless a direct import, compile, or test failure in the active files requires it.",
                        ],
                        "carry_forward_patches": patches,
                        "repair_source_subtask_id": str(test_artifact.get("subtask_id") or "").strip(),
                        "repair_source_worker_id": str(test_artifact.get("worker_id") or "").strip(),
                        "repair_changed_files": active_files,
                        "repair_requires_production_followup": paired_source_files,
                        "repair_requires_test_followup": test_files,
                        "repair_verification_first": True,
                        "deterministic_partial_diff_repair": True,
                        "write_required": False,
                        "scope_note": (
                            "ACA detected complementary partial diffs: one production-only patch for "
                            + source_text
                            + " and one test-only patch for "
                            + test_text
                            + ". Both patches are applied before this worker starts; verify/fix the combined source+test diff."
                        ),
                    }
                ],
                "risks": [
                    "If either preserved patch no longer applies, ACA will fail closed and retry with fresh repair evidence."
                ],
                "tests": ["Run the narrowest deterministic verification for " + test_text + "."],
            }
    return None


def _mark_manager_planning_started(ctx: RunContext) -> None:
    from src.tandem_agents.runtime.run_output import set_status

    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="planning",
        phase_detail="manager planning",
        phase_role="manager",
        run_status="running",
    )


def _manager_prompt_timeout_seconds(cfg: Any) -> float:
    raw = str(getattr(cfg, "env", {}).get("ACA_MANAGER_PROMPT_TIMEOUT_SECONDS", "") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_MANAGER_PROMPT_TIMEOUT_SECONDS=%r", raw)
    return 300.0


def _deterministic_testless_repair_active(subtasks: list[dict[str, Any]]) -> bool:
    return any(
        bool(subtask.get("deterministic_testless_repair") or subtask.get("deterministic_partial_diff_repair"))
        for subtask in subtasks
    )


def _repair_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_required_after_prescreen(subtask: dict[str, Any]) -> bool:
    if subtask.get("repair_verification_first"):
        return False
    return not bool(subtask.get("pre_satisfied"))


def _narrow_carried_partial_diff_subtask(subtask: dict[str, Any]) -> None:
    if not subtask.get("carry_forward_patch"):
        return
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    active_files = list(changed_files)
    production_followup_files = _test_only_partial_production_followup_files(subtask)
    if production_followup_files:
        active_files = list(dict.fromkeys([*production_followup_files, *changed_files]))
    required_test_files = _testless_partial_required_test_files(subtask)
    if not required_test_files:
        required_test_files = _source_partial_declared_test_followup_files(subtask)
    if required_test_files:
        active_files = list(dict.fromkeys([*changed_files, *required_test_files]))
    verify_first_partial = _partial_diff_retry_should_verify_first(subtask, changed_files)
    if verify_first_partial:
        subtask["write_required"] = False
        subtask["repair_verification_first"] = True
    if changed_files:
        previous_files = sorted(_subtask_declared_files(subtask).difference(active_files))
        subtask["files"] = list(dict.fromkeys(active_files))
        subtask["target_files"] = list(dict.fromkeys(active_files))
        if previous_files:
            subtask["repair_deferred_files"] = list(
                dict.fromkeys([*(subtask.get("repair_deferred_files") or []), *previous_files])
            )
        if production_followup_files:
            subtask["repair_requires_production_followup"] = production_followup_files
        if required_test_files:
            subtask["repair_requires_test_followup"] = required_test_files
    context = _compact_partial_diff_repair_context(str(subtask.get("repair_worker_output_excerpt") or ""))
    target_text = ", ".join(active_files) if active_files else "the files touched by the preserved patch"
    original_criteria = [
        str(entry).strip()
        for entry in (subtask.get("acceptance_criteria") or [])
        if str(entry).strip()
    ]
    if original_criteria and "repair_deferred_acceptance_criteria" not in subtask:
        subtask["repair_deferred_acceptance_criteria"] = original_criteria
    criteria = [
        "Resolve the recovered partial-diff blocker by finishing the preserved patch in: " + target_text + ".",
        "Run the narrowest deterministic verification for the preserved files, or record the exact unavailable command/blocker.",
    ]
    if context:
        criteria.insert(1, "Recovered blocker context: " + context)
    if verify_first_partial:
        criteria = [
            "Apply and inspect the preserved source+test patch in: " + target_text + ".",
            "Run the narrow deterministic verification first; if it passes, return a terminal completion note without making another mandatory edit.",
            "If verification fails, fix only the failing behavior in the preserved source/test files and rerun the narrow verification or record the exact blocker.",
        ]
        if context:
            criteria.insert(1, "Recovered blocker context: " + context)
    elif production_followup_files:
        criteria.insert(
            1,
            "The preserved diff is test-only; read and complete the required production behavior in: "
            + ", ".join(production_followup_files)
            + " before changing or adding more tests.",
        )
        criteria.insert(
            2,
            "Do not mark this repair complete with a test-only diff; either leave a semantic production diff in "
            + ", ".join(production_followup_files)
            + " or report a concrete blocker explaining why no production edit is safe.",
        )
        criteria.insert(
            3,
            "After applying the carried test patch, make the first new repair edit in the production follow-up file before expanding the test patch.",
        )
        criteria.append(
            "Do not expand into unrelated manager scope beyond the preserved patch and required production follow-up files."
        )
    elif required_test_files:
        criteria.insert(
            1,
            "The preserved diff changed only non-test files or timed out before required coverage was added; read and edit the required test file(s) in: "
            + ", ".join(required_test_files)
            + " before continuing production-only work.",
        )
        criteria.insert(
            2,
            "Do not mark this repair complete until the diff includes real coverage in "
            + ", ".join(required_test_files)
            + " and the narrow verification has run or a concrete blocker is recorded.",
        )
        criteria.append(
            "Do not expand into unrelated manager scope beyond the preserved patch and required test follow-up files."
        )
    else:
        criteria.insert(
            1 if not context else 2,
            "Do not expand into deferred manager scope unless a direct import or compile/test failure in the preserved files requires it.",
        )
    subtask["acceptance_criteria"] = criteria
    subtask["write_required"] = not verify_first_partial
    original_goal = str(subtask.get("goal") or "").strip()
    if original_goal and "repair_original_goal" not in subtask:
        subtask["repair_original_goal"] = original_goal
    if verify_first_partial:
        subtask["goal"] = (
            "Verify the preserved source+test partial worker diff and fix only narrow verification failures for "
            + target_text
            + "."
        )
    else:
        subtask["goal"] = (
            "Finish the preserved partial worker diff and obtain a terminal worker verdict for "
            + target_text
            + "."
        )


def _compact_rejected_partial_diff_subtask(subtask: dict[str, Any]) -> None:
    if not subtask.get("discarded_partial_diff_patch"):
        return
    target_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (subtask.get("repair_parent_target_files") or list(_subtask_declared_files(subtask)))
        )
        if rel_path
    ]
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (subtask.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    original_criteria = [
        str(entry).strip()
        for entry in (subtask.get("acceptance_criteria") or [])
        if str(entry).strip()
    ]
    if original_criteria and "repair_deferred_acceptance_criteria" not in subtask:
        subtask["repair_deferred_acceptance_criteria"] = original_criteria
    target_text = ", ".join(target_files) if target_files else "the active parent task target files"
    changed_text = ", ".join(changed_files) if changed_files else "none recorded"
    failure_summary = str(subtask.get("repair_failure_summary") or "").strip()
    criteria = [
        "Replace the rejected or incomplete partial-diff approach before expanding scope.",
        "Keep repair edits scoped to the parent task target files: " + target_text + ".",
        "Use the rejected diff only as failure evidence; do not read, apply, copy, or reference its patch artifact.",
        "Rejected diff touched: " + changed_text + ".",
        "Add one deterministic production-backed assertion or implementation slice that addresses the failure summary.",
        "Run the narrowest deterministic verification for the active target files, or record the exact unavailable command/blocker.",
    ]
    if failure_summary:
        criteria.insert(1, "Failure summary: " + failure_summary + ".")
    subtask["acceptance_criteria"] = criteria
    original_goal = str(subtask.get("goal") or "").strip()
    if original_goal and "repair_original_goal" not in subtask:
        subtask["repair_original_goal"] = original_goal
    subtask["goal"] = (
        "Replace the rejected partial worker diff with a narrow production-backed repair for "
        + target_text
        + "."
    )


def _constrain_extra_partial_diff_repair_subtasks(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
) -> None:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return
    attempt = _repair_int(repair.get("attempt"))
    base_max_loops = _repair_int(repair.get("base_max_loops"))
    if not attempt or not base_max_loops or attempt <= base_max_loops:
        return
    carried = [subtask for subtask in subtasks if subtask.get("carry_forward_patch") or subtask.get("carry_forward_patches")]
    rejected = [
        subtask
        for subtask in subtasks
        if subtask.get("discarded_partial_diff_patch") or subtask.get("repair_parent_target_files")
    ]
    candidates = carried or rejected
    if not candidates:
        return
    chosen = candidates[0]
    if len(subtasks) > 1:
        subtasks[:] = [chosen]
    parent_target_files = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (chosen.get("repair_parent_target_files") or [])
        )
        if rel_path
    ]
    if chosen.get("discarded_partial_diff_patch") and parent_target_files:
        chosen["files"] = list(dict.fromkeys(parent_target_files))
        chosen["target_files"] = list(dict.fromkeys(parent_target_files))
        existing_scope_note = str(chosen.get("scope_note") or "").strip()
        parent_note = (
            "ACA kept this extra repair attempt on the parent task target files because the preserved "
            "partial diff was rejected or incomplete. Active repair targets are the parent task targets: "
            + ", ".join(parent_target_files)
            + "."
        )
        if parent_note not in existing_scope_note:
            chosen["scope_note"] = f"{existing_scope_note}\n{parent_note}".strip()
        return
    changed_files = [
        rel_path
        for rel_path in (_normalize_repo_relative_path(raw_path) for raw_path in (chosen.get("repair_changed_files") or []))
        if rel_path and rel_path != "__aca_temp_probe.txt"
    ]
    _narrow_carried_partial_diff_subtask(chosen)
    existing_scope_note = str(chosen.get("scope_note") or "").strip()
    narrow_note = (
        "ACA narrowed this extra repair attempt to the carried partial-diff subtask so the worker "
        "finishes the preserved files before the manager can expand into new swarm slices."
    )
    active_targets = [
        rel_path
        for rel_path in (
            _normalize_repo_relative_path(raw_path)
            for raw_path in (chosen.get("target_files") or chosen.get("files") or changed_files)
        )
        if rel_path
    ]
    if active_targets:
        narrow_note += " Active repair targets are: " + ", ".join(active_targets) + "."
    if chosen.get("repair_requires_production_followup"):
        narrow_note += (
            " Production follow-up targets required for the test-only partial diff: "
            + ", ".join(chosen["repair_requires_production_followup"])
            + "."
        )
    if chosen.get("repair_deferred_files"):
        narrow_note += " Broader manager files are deferred until the preserved patch is terminal: " + ", ".join(chosen["repair_deferred_files"]) + "."
    if narrow_note not in existing_scope_note:
        chosen["scope_note"] = f"{existing_scope_note}\n{narrow_note}".strip()


def _extra_partial_diff_repair_active(ctx: RunContext) -> bool:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return False
    attempt = _repair_int(repair.get("attempt"))
    base_max_loops = _repair_int(repair.get("base_max_loops"))
    return bool(attempt and base_max_loops and attempt > base_max_loops)


def _merge_or_defer_sticky_expected_files(
    ctx: RunContext,
    subtasks: list[dict[str, Any]],
    current_expected_files: list[str],
    sticky_expected_files: list[str],
) -> None:
    sticky_missing_from_plan = [path for path in sticky_expected_files if path not in current_expected_files]
    if not sticky_missing_from_plan or not subtasks:
        return
    first = subtasks[0]
    partial_diff_repair = bool(first.get("carry_forward_patch") or first.get("carry_forward_patches"))
    rejected_partial_diff_repair = bool(
        first.get("discarded_partial_diff_patch") or first.get("repair_parent_target_files")
    )
    production_followup_files = _test_only_partial_production_followup_files(
        first,
        sticky_missing_from_plan,
    )
    if partial_diff_repair:
        if production_followup_files:
            _append_unique_repo_paths(first, production_followup_files)
            _narrow_carried_partial_diff_subtask(first)
            existing_scope_note = str(first.get("scope_note") or "").strip()
            production_note = (
                "ACA kept sticky production follow-up targets active because the preserved partial diff is test-only: "
                + ", ".join(production_followup_files)
                + "."
            )
            if production_note not in existing_scope_note:
                first["scope_note"] = f"{existing_scope_note}\n{production_note}".strip()
        deferred = [
            str(entry).strip()
            for entry in (first.get("repair_deferred_files") or [])
            if str(entry).strip()
        ]
        for path in (path for path in sticky_missing_from_plan if path not in production_followup_files):
            if path not in deferred:
                deferred.append(path)
        if deferred:
            first["repair_deferred_files"] = deferred
        return
    if rejected_partial_diff_repair:
        parent_target_files = [
            rel_path
            for rel_path in (
                _normalize_repo_relative_path(raw_path)
                for raw_path in (first.get("repair_parent_target_files") or [])
            )
            if rel_path
        ]
        if parent_target_files:
            scoped_targets = list(dict.fromkeys(parent_target_files))
            first["files"] = scoped_targets
            first["target_files"] = scoped_targets
        deferred = [
            str(entry).strip()
            for entry in (first.get("repair_deferred_files") or [])
            if str(entry).strip()
        ]
        for path in sticky_missing_from_plan:
            if path not in deferred:
                deferred.append(path)
        if deferred:
            first["repair_deferred_files"] = deferred
        existing_scope_note = str(first.get("scope_note") or "").strip()
        deferred_note = (
            "ACA deferred sticky expected files from earlier attempts because this retry is replacing a rejected "
            "partial diff and must stay scoped to the parent repair targets: "
            + ", ".join(sticky_missing_from_plan)
            + "."
        )
        if deferred_note not in existing_scope_note:
            first["scope_note"] = f"{existing_scope_note}\n{deferred_note}".strip()
        return
    for key in ("files", "target_files"):
        values = [str(entry).strip() for entry in (first.get(key) or []) if str(entry).strip()]
        for path in sticky_missing_from_plan:
            if path not in values:
                values.append(path)
        first[key] = values
    existing_scope_note = str(first.get("scope_note") or "").strip()
    sticky_note = (
        "ACA kept these expected files from an earlier retry attempt because later manager plans "
        "must not narrow the run contract: "
        + ", ".join(sticky_missing_from_plan)
        + "."
    )
    first["scope_note"] = f"{existing_scope_note}\n{sticky_note}".strip()
    if production_followup_files and (first.get("carry_forward_patch") or first.get("carry_forward_patches")):
        _narrow_carried_partial_diff_subtask(first)


def _completed_repair_subtask_ids(ctx: RunContext) -> set[str]:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    raw_ids = repair.get("completed_subtask_ids") if isinstance(repair, dict) else []
    if not isinstance(raw_ids, list):
        return set()
    return {str(item).strip() for item in raw_ids if str(item).strip()}


def _namespace_repair_retry_subtask_ids(ctx: RunContext, subtasks: list[dict[str, Any]]) -> None:
    """Avoid retry-plan ID collisions with completed subtasks from earlier attempts."""
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    if not isinstance(repair, dict) or not repair.get("partial_diff_artifacts"):
        return
    completed_ids = _completed_repair_subtask_ids(ctx)
    if not completed_ids:
        return
    attempt = max(1, _repair_int(repair.get("attempt")))
    seen: set[str] = set()
    for index, subtask in enumerate(subtasks, start=1):
        original_id = str(subtask.get("id") or f"subtask-{index}").strip() or f"subtask-{index}"
        new_id = original_id
        if original_id in completed_ids or original_id in seen:
            base = f"repair-attempt-{attempt}-{original_id}"
            new_id = base
            suffix = 2
            while new_id in seen:
                new_id = f"{base}-{suffix}"
                suffix += 1
            subtask["repair_original_subtask_id"] = original_id
            subtask["id"] = new_id
            note = (
                "ACA renamed this repair subtask from "
                f"{original_id} to {new_id} so completed-subtask carry-forward from an earlier "
                "attempt cannot skip or overwrite the active repair worker."
            )
            existing_scope_note = str(subtask.get("scope_note") or "").strip()
            if note not in existing_scope_note:
                subtask["scope_note"] = f"{existing_scope_note}\n{note}".strip()
        seen.add(new_id)


def _completed_repair_worker_results(
    ctx: RunContext,
    recorded_subtask_ids: set[str],
) -> list[dict[str, Any]]:
    """Return successful prior-attempt worker results missing from this retry plan."""
    from src.tandem_agents.core.execution import runner_core as _rc  # noqa: PLC0415

    completed_ids = _completed_repair_subtask_ids(ctx)
    if not completed_ids:
        return []
    missing_ids = {subtask_id for subtask_id in completed_ids if subtask_id not in recorded_subtask_ids}
    if not missing_ids:
        return []
    workers = ctx.blackboard.get("workers") if isinstance(ctx.blackboard, dict) else []
    if not isinstance(workers, list):
        return []
    carried: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in reversed(workers):
        if not isinstance(result, dict):
            continue
        subtask_id = str(result.get("subtask_id") or "").strip()
        if not subtask_id or subtask_id not in missing_ids or subtask_id in seen:
            continue
        status = _rc._normalized_text(result.get("status"))
        if status not in {"completed", "skipped_existing", "tolerated_failure"} and not result.get("verified_existing"):
            continue
        cloned = dict(result)
        cloned["worker_id"] = f"repo-check-{subtask_id}"
        cloned["subtask_index"] = 0
        cloned["status"] = "skipped_existing"
        cloned["returncode"] = 0
        cloned["worktree"] = str(ctx.repo_path)
        cloned["write_required"] = False
        cloned["verified_existing"] = True
        cloned["output_excerpt"] = (
            "Subtask carried forward from a completed repair-loop worker even though "
            "the retry manager narrowed the current plan away from this subtask."
        )
        carried.append(cloned)
        seen.add(subtask_id)
    carried.reverse()
    return carried


def run_manager_prompt(ctx: RunContext) -> None:
    """Execute the manager (planning) prompt and populate ctx.manager_plan.

    Mutates:
        ctx.manager_plan   -- parsed plan dict (summary, subtasks, risks, tests)
        ctx.blackboard     -- updated with manager_plan key
    """
    from src.tandem_agents.core.engine.engine import engine_env, engine_session_provider_model
    from src.tandem_agents.core.execution.worker import stream_tandem_prompt
    from src.tandem_agents.core.repository.repo_context import repo_context_for_task
    from src.tandem_agents.core.engine.prompts import build_manager_prompt
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard, write_status
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot

    manager_model_selection = engine_session_provider_model(ctx.cfg, "manager")
    manager_provider = manager_model_selection["provider"]
    manager_model = manager_model_selection["model"]
    repo_context = repo_context_for_task(
        ctx.cfg,
        ctx.repo_path,
        ctx.task,
        artifact_path=ctx.layout["artifacts"] / "repo_context_bundle.json",
    )
    ctx.blackboard["repo_context"] = {
        "source": repo_context.source,
        "fallback_used": repo_context.fallback_used,
        "error": repo_context.error,
        "artifact_path": repo_context.artifact_path,
        "path_scope": repo_context.path_scope,
        "required_files": repo_context.required_files or [],
        "index_source": repo_context.index_source,
        "index_status": repo_context.index_status,
        "index_error": repo_context.index_error,
    }
    if _apply_repo_context_required_files_to_task(ctx.task, repo_context.required_files):
        ctx.blackboard["repo_context"]["required_files_applied_as_target_files"] = True
        ctx.status["repo_context_required_files_applied_as_target_files"] = True
    ctx.status.setdefault("artifacts", {})
    if repo_context.artifact_path:
        ctx.status["artifacts"]["repo_context_bundle"] = repo_context.artifact_path
    ctx.status["repo_context"] = dict(ctx.blackboard["repo_context"])
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.layout["run_dir"], ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    manager_prompt = build_manager_prompt(
        ctx.run_id,
        ctx.task,
        ctx.repo,
        ctx.cfg,
        repo_context=repo_context.text,
        previous_feedback=getattr(ctx, "_previous_feedback", None),
    )

    _mark_manager_planning_started(ctx)
    append_event(
        ctx.layout["events"],
        "manager.started",
        ctx.run_id,
        {"role": "manager", "repo_context": dict(ctx.blackboard["repo_context"])},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )

    deterministic_repair_plan = _deterministic_testless_partial_diff_repair_plan(ctx)
    if deterministic_repair_plan:
        ctx.manager_plan = deterministic_repair_plan
        ctx.blackboard["manager_plan"] = ctx.manager_plan
        ctx.blackboard["manager_deterministic_repair_plan"] = {
            "kind": "worker_off_track_testless_diff",
            "subtask_count": len(deterministic_repair_plan.get("subtasks") or []),
        }
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        write_status(ctx.layout["status"], ctx.status)
        append_event(
            ctx.layout["events"],
            "manager.deterministic_repair_plan",
            ctx.run_id,
            ctx.blackboard["manager_deterministic_repair_plan"],
            task_id=ctx.task.get("task_id"),
            role="manager",
            repo={"path": ctx.repo.get("path")},
        )
        return {
            "returncode": 0,
            "stdout": json.dumps(deterministic_repair_plan),
            "stderr": "",
            "engine": {"skipped": True, "reason": "worker_off_track_testless_diff"},
        }

    logger.info("Running manager prompt (run_id=%s)", ctx.run_id)
    timeout_seconds = _manager_prompt_timeout_seconds(ctx.cfg)

    def _stream_manager_prompt() -> dict[str, Any]:
        return stream_tandem_prompt(
            ctx.cfg,
            role="manager",
            prompt=manager_prompt,
            cwd=ctx.repo_path,
            provider=manager_provider,
            model=manager_model,
            env=engine_env(ctx.cfg),
            log_path=ctx.layout["logs"] / "manager.log",
            config_path=None,
        )

    with _rc._coordination_heartbeat(ctx, phase="planning"):
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aca-manager")
        future = executor.submit(_stream_manager_prompt)
        try:
            manager_result = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            message = (
                "ENGINE_PROMPT_TIMEOUT: ACA manager planning prompt exceeded "
                f"{timeout_seconds:.0f}s without producing a plan. The run will use "
                "contract-based fallback planning instead of remaining stuck in planning."
            )
            try:
                log_path = ctx.layout["logs"] / "manager.log"
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except Exception:
                logger.debug("Failed to append manager watchdog message", exc_info=True)
            manager_result = {
                "role": "manager",
                "returncode": 1,
                "stdout": message,
                "stderr": "",
                "log_path": str(ctx.layout["logs"] / "manager.log"),
                "failure_reason": message,
                "blocker_kind": "manager_prompt_timeout",
                "recovery_action": (
                    "Use ACA's contract-based fallback planner for this attempt; inspect manager.log "
                    "and engine run state if manager prompts repeatedly time out."
                ),
                "engine": {
                    "stream_reason": "manager_prompt_timeout",
                    "timeout_seconds": timeout_seconds,
                },
            }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    parsed_plan, invalid_plan_reason = _manager_plan_from_stdout(str(manager_result.get("stdout") or ""))
    if invalid_plan_reason:
        excerpt = str(manager_result.get("stdout") or "").strip()[:1000]
        ctx.manager_plan = {
            "summary": excerpt,
            "subtasks": [],
            "risks": [invalid_plan_reason],
            "tests": [],
        }
        ctx.blackboard["manager_plan"] = ctx.manager_plan
        ctx.blackboard["manager_invalid_plan"] = {
            "reason": invalid_plan_reason,
            "stdout_excerpt": excerpt,
        }
        manager_result["returncode"] = 1
        manager_result["failure_reason"] = invalid_plan_reason
        manager_result["blocker_kind"] = "manager_invalid_plan"
        manager_result["recovery_action"] = (
            "Use ACA's contract-based fallback planner, then block only if no safe repo targets can be inferred."
        )
        ctx.status = set_status(
            ctx.status,
            ctx.layout,
            phase="planning",
            phase_detail=f"{invalid_plan_reason} Falling back to contract-based planning.",
            blocker=(False, None, None, None),
        )
        append_event(
            ctx.layout["events"],
            "manager.invalid_plan",
            ctx.run_id,
            {"reason": invalid_plan_reason, "stdout_excerpt": excerpt, "recoverable": True},
            task_id=ctx.task.get("task_id"),
            role="manager",
            repo={"path": ctx.repo.get("path")},
        )
    else:
        ctx.manager_plan = parsed_plan or {}
        _sanitize_partial_diff_artifact_paths_in_plan(ctx.manager_plan)
        ctx.blackboard["manager_plan"] = ctx.manager_plan
    if manager_result.get("engine") or manager_result.get("blocker_kind"):
        ctx.blackboard["manager_engine"] = {
            "engine": manager_result.get("engine") or {},
            "failure_reason": manager_result.get("failure_reason") or "",
            "blocker_kind": manager_result.get("blocker_kind") or "",
            "recovery_action": manager_result.get("recovery_action") or "",
        }
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    return manager_result


def pre_screen_subtasks(ctx: RunContext) -> bool:
    """Pre-screen planned subtasks against the repository.

    Separates each subtask into ``pending`` (needs work) or ``skipped_existing``
    (already satisfied by current repo state).

    Mutates:
        ctx.planned_subtasks   -- annotated with pre_satisfied and existing_files
        ctx.pending_subtasks   -- subtasks that need worker execution
        ctx.worker_results     -- extended with skipped-existing results
        ctx.expected_repo_files
        ctx.repo_validation
        ctx.blackboard

    Returns:
        True if all subtasks are pre-satisfied (no workers needed).
        False if there is work to do.
    """
    from src.tandem_agents.core.repository.repo_truth import (
        file_is_readable,
        subtask_satisfied,
    )
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot
    from pathlib import Path
    from src.tandem_agents.core.task_contract import task_plan_validation

    repo_path = ctx.repo_path
    discovered_files, subtasks = _prepare_subtasks(ctx)
    _carry_forward_partial_diff_artifacts(ctx, subtasks)
    _constrain_extra_partial_diff_repair_subtasks(ctx, subtasks)
    _append_deferred_repair_subtasks(ctx, subtasks)
    _namespace_repair_retry_subtask_ids(ctx, subtasks)
    _align_python_test_targets_to_repo_conventions(repo_path, subtasks)
    _split_dense_serial_subtasks(ctx, subtasks)

    ctx.planned_subtasks = subtasks
    ctx.blackboard["subtasks"] = ctx.planned_subtasks
    ctx.pending_subtasks = []
    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    deterministic_testless_repair = _deterministic_testless_repair_active(ctx.planned_subtasks)
    if deterministic_testless_repair:
        ctx.blackboard["expected_repo_files"] = list(current_expected_files)
    else:
        sticky_expected_files = _rc._sticky_expected_repo_files(ctx.blackboard, current_expected_files)
        _merge_or_defer_sticky_expected_files(
            ctx,
            ctx.planned_subtasks,
            current_expected_files,
            sticky_expected_files,
        )
    plan_validation = task_plan_validation(ctx.task, subtasks)
    ctx.blackboard["task_plan_validation"] = plan_validation
    force_worker_execution = (
        _remote_code_task_requires_worker_execution(ctx.task)
        or bool(getattr(ctx, "_manager_fallback_required", False))
        or _rc._task_mentions_external_pr_candidates(ctx.task)
    )
    completed_repair_subtask_ids = _completed_repair_subtask_ids(ctx)
    if not plan_validation.get("ok", True):
        blocker_kind = str(plan_validation.get("blocker_kind") or "contract_incomplete")
        blocker_message = str(plan_validation.get("blocker_message") or "Subtask plan is incomplete or unsafe.")
        ctx.status = set_status(
            ctx.status,
            ctx.layout,
            phase="planning",
            phase_detail=blocker_message,
            run_status="blocked",
            blocker=(True, blocker_kind, blocker_message, "manager"),
        )
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        logger.warning(
            "Manager plan blocked during pre-screen (run_id=%s): %s",
            ctx.run_id,
            blocker_message,
        )
        return False

    for subtask in ctx.planned_subtasks:
        readable_existing = [
            rel_path
            for rel_path in (subtask.get("files") or [])
            if str(rel_path or "").strip()
            and (repo_path / str(rel_path).strip()).exists()
            and (repo_path / str(rel_path).strip()).is_file()
            and file_is_readable(repo_path / str(rel_path).strip())
        ]
        subtask["existing_files"] = readable_existing
        carried_forward_success = (
            str(subtask.get("id") or "").strip() in completed_repair_subtask_ids
            and subtask_satisfied(repo_path, subtask)
        )
        repair_requires_worker = bool(
            subtask.get("carry_forward_patch")
            or subtask.get("carry_forward_patches")
            or subtask.get("discarded_partial_diff_patch")
        )
        subtask["pre_satisfied"] = (
            False
            if repair_requires_worker
            else (
                True
                if carried_forward_success
                else (False if force_worker_execution else subtask_satisfied(repo_path, subtask))
            )
        )
        subtask["write_required"] = _write_required_after_prescreen(subtask)

        if subtask["pre_satisfied"]:
            skip_reason = (
                "carried forward from a completed repair-loop worker"
                if carried_forward_success
                else "already satisfied"
            )
            skipped_result = {
                "worker_id": f"repo-check-{subtask['id']}",
                "subtask_index": 0,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "skipped_existing",
                "returncode": 0,
                "worktree": str(repo_path),
                "log_path": "",
                "output_excerpt": (
                    "Subtask skipped because it was "
                    f"{skip_reason} and its target files are readable in the base repository."
                ),
                "write_required": False,
                "verified_existing": True,
            }
            _rc._record_worker_result(ctx.blackboard, ctx.worker_results, skipped_result)
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == subtask["id"]:
                    item["status"] = "skipped_existing"
                    item["write_required"] = False
                    break
            append_event(
                ctx.layout["events"],
                "worker.skipped",
                ctx.run_id,
                {"subtask_id": subtask["id"], "reason": skip_reason},
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
        else:
            ctx.pending_subtasks.append(subtask)

    recorded_subtask_ids = {
        str(result.get("subtask_id") or "").strip()
        for result in ctx.worker_results
        if str(result.get("subtask_id") or "").strip()
    }
    for carried_result in _completed_repair_worker_results(ctx, recorded_subtask_ids):
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, carried_result)
        append_event(
            ctx.layout["events"],
            "worker.skipped",
            ctx.run_id,
            {
                "subtask_id": carried_result["subtask_id"],
                "reason": "carried forward from a previous repair-loop plan",
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    if deterministic_testless_repair:
        ctx.expected_repo_files = list(current_expected_files)
        ctx.blackboard["expected_repo_files"] = list(ctx.expected_repo_files)
    else:
        ctx.expected_repo_files = _rc._sticky_expected_repo_files(
            ctx.blackboard,
            current_expected_files,
        )
    ctx.repo_validation = _rc._deterministic_repo_validation(repo_path, ctx.expected_repo_files)
    ctx.blackboard["repo_validation"] = ctx.repo_validation

    ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    all_pre_satisfied = (
        not ctx.pending_subtasks
        and _rc._all_subtasks_verified_existing(ctx.planned_subtasks, ctx.worker_results, ctx.repo_validation, ctx.task)
    )
    if all_pre_satisfied:
        logger.info(
            "All %d subtask(s) pre-satisfied by existing repo files; skipping worker execution.",
            len(ctx.planned_subtasks),
        )
    return all_pre_satisfied
