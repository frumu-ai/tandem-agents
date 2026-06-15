"""phases/worker_dispatch.py -- Local worker pool execution and result collection.

This module owns the worker dispatch phase:
1. Register workers with the coordination store
2. Spin up a heartbeat thread to keep leases alive during execution
3. Execute the local ThreadPoolExecutor worker pool via ``_execute_local_worker_pool``
4. Collect results and apply tolerated-failure logic
5. Clean up stale worker registrations on completion

All worker state is written into the RunContext. No return value — the
caller continues with ctx.worker_results after this returns.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.worker_dispatch")


_TERMINAL_WORKER_BLOCKER_KINDS = {
    "approval_failed",
    "github_context_unavailable",
    "unsupported_task",
    "worker_corrupt_diff",
    "worker_off_track",
    "worker_runaway_diff",
    "worker_unproductive_diff",
    "worker_no_progress",
    "worker_no_diff",
}

_UNPRODUCTIVE_DIFF_MARKERS = (
    "TODO(worker-blocker)",
    "panic!(\"blocked:",
    "panic!('blocked:",
    "blocked: production-path regression coverage",
    "production-path regression coverage was not added or verified",
)


def _is_test_path(path: str) -> bool:
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    return (
        lowered.startswith("tests/")
        or "/tests/" in f"/{lowered}"
        or lowered.endswith((
            "_test.rs",
            "_tests.rs",
            "_test.py",
            ".test.ts",
            ".test.tsx",
            ".spec.ts",
            ".spec.tsx",
        ))
    )


def _text_mentions_test_work(value: Any) -> bool:
    text = str(value or "").lower()
    return any(word in text for word in ("test", "tests", "coverage", "regression"))


def _subtask_required_test_files(subtask: dict[str, Any]) -> list[str]:
    files = subtask.get("files") or subtask.get("target_files") or []
    if not isinstance(files, list):
        files = [files]
    return [str(path).strip() for path in files if _is_test_path(str(path))]


def _subtask_requires_test_changes(subtask: dict[str, Any]) -> bool:
    if not _subtask_required_test_files(subtask):
        return False
    text_parts: list[Any] = [
        subtask.get("title"),
        subtask.get("goal"),
        subtask.get("scope_note"),
    ]
    for field in ("acceptance_criteria", "deliverables"):
        value = subtask.get(field)
        if isinstance(value, (list, tuple, set)):
            text_parts.extend(value)
        elif value:
            text_parts.append(value)
    return any(_text_mentions_test_work(part) for part in text_parts)


def _worker_testless_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS=%r", raw)
    return 180.0


def _worker_comment_only_diff_abort_seconds(ctx: RunContext) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_COMMENT_ONLY_DIFF_ABORT_SECONDS") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_COMMENT_ONLY_DIFF_ABORT_SECONDS=%r", raw)
    return 120.0


def _added_diff_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if not line.startswith("+") or line.startswith(("+++", "+++ ")):
            continue
        lines.append(line[1:])
    return lines


def _removed_diff_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in str(diff_text or "").splitlines():
        if not line.startswith("-") or line.startswith(("---", "--- ")):
            continue
        lines.append(line[1:])
    return lines


def _diff_has_unproductive_marker(diff_text: str) -> bool:
    return any(marker in str(diff_text or "") for marker in _UNPRODUCTIVE_DIFF_MARKERS)


def _diff_is_comment_only(diff_text: str) -> bool:
    added = [line.strip() for line in _added_diff_lines(diff_text) if line.strip()]
    if not added:
        return False
    comment_prefixes = ("//", "#", "/*", "*", "*/", "//!", "///")
    return all(line.startswith(comment_prefixes) for line in added)


def _diff_has_tautological_boolean_assertion(diff_text: str) -> bool:
    code_lines = [
        line.strip()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("//", "#", "/*", "*", "*/"))
    ]
    if not code_lines:
        return False
    declared_true: set[str] = set()
    non_tautological: list[str] = []
    for line in code_lines:
        match = re.fullmatch(r"(?:let\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*true\s*;", line)
        if match:
            declared_true.add(match.group(1))
            continue
        match = re.fullmatch(r"assert!\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;", line)
        if match and match.group(1) in declared_true:
            continue
        if re.fullmatch(r"assert!\(\s*true\s*\)\s*;", line):
            continue
        non_tautological.append(line)
    return bool(declared_true or code_lines) and not non_tautological


def _diff_is_string_only_change(diff_text: str) -> bool:
    added = [line.strip() for line in _added_diff_lines(diff_text) if line.strip()]
    removed = [line.strip() for line in _removed_diff_lines(diff_text) if line.strip()]
    if not added or not removed:
        return False
    string_line = re.compile(r'^[A-Za-z0-9_"\':,\s.\-{}()\[\]]*".*"[A-Za-z0-9_"\':,\s.\-{}()\[\]]*$')
    if not all(string_line.match(line) for line in added + removed):
        return False
    normalize = lambda line: re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    return sorted(normalize(line) for line in added) == sorted(normalize(line) for line in removed)


def _diff_is_local_string_oracle_test(diff_text: str) -> bool:
    code_lines = [
        line.strip()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("//", "#", "/*", "*", "*/", "//!", "///"))
    ]
    if not code_lines:
        return False
    local_strings: set[str] = set()
    meaningful_asserts = 0
    for line in code_lines:
        if line in {"{", "}", "};"}:
            continue
        if line.startswith("#[") or re.match(r"(?:async\s+)?fn\s+[A-Za-z_][A-Za-z0-9_]*\s*\(", line):
            continue
        match = re.fullmatch(r"let\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\"(?:[^\"\\]|\\.)*\"\s*;", line)
        if match:
            local_strings.add(match.group(1))
            continue
        match = re.fullmatch(
            r"assert!\(\s*([A-Za-z_][A-Za-z0-9_]*)\.contains\(\s*\"(?:[^\"\\]|\\.)*\"\s*\)\s*\)\s*;",
            line,
        )
        if match and match.group(1) in local_strings:
            meaningful_asserts += 1
            continue
        match = re.fullmatch(
            r"assert_ne!\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;",
            line,
        )
        if match and match.group(1) in local_strings and match.group(2) in local_strings:
            meaningful_asserts += 1
            continue
        return False
    return bool(local_strings) and meaningful_asserts >= 2


def _diff_has_placeholder_noop_test(diff_text: str) -> bool:
    added = [
        line.strip().lower()
        for line in _added_diff_lines(diff_text)
        if line.strip() and not line.strip().startswith(("+++", "#["))
    ]
    if not added:
        return False
    placeholder_terms = (
        "placeholder",
        "must be replaced",
        "replace with",
        "before completion",
        "before merging",
        "not implemented",
    )
    has_placeholder_language = any(any(term in line for term in placeholder_terms) for line in added)
    has_noop_assertion = any(re.fullmatch(r"assert!\(\s*true\s*\)\s*;", line) for line in added)
    return has_noop_assertion and has_placeholder_language


def _diff_missing_production_function_calls(worktree: Path, diff_text: str, changed_files: list[str]) -> list[str]:
    if not changed_files or not all(_is_test_path(path) for path in changed_files):
        return []
    added_lines = _added_diff_lines(diff_text)
    defined_in_diff: set[str] = set()
    candidates: set[str] = set()
    call_pattern = re.compile(r"(?<![\w.!])([A-Za-z_][A-Za-z0-9_]*)\s*(?=\()")
    helper_markers = (
        "github",
        "project",
        "projects",
        "readiness",
        "intake",
        "schema",
        "drift",
        "divergence",
        "diagnostic",
    )
    ignored_calls = {
        "Some",
        "None",
        "Ok",
        "Err",
        "String",
        "Vec",
        "HashMap",
        "HashSet",
        "BTreeMap",
        "BTreeSet",
        "Option",
        "Result",
    }
    for raw_line in added_lines:
        line = raw_line.strip()
        if not line or line.startswith(("//", "#", "/*", "*", "*/", "//!", "///")):
            continue
        definition = re.match(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if definition:
            defined_in_diff.add(definition.group(1))
            continue
        for match in call_pattern.finditer(line):
            name = match.group(1)
            if name in ignored_calls or name in defined_in_diff:
                continue
            lowered = name.lower()
            if "_" not in name:
                continue
            if any(marker in lowered for marker in helper_markers):
                candidates.add(name)
    if not candidates:
        return []
    changed_set = {str(path or "").strip().replace("\\", "/").strip("/") for path in changed_files}
    missing: list[str] = []
    for name in sorted(candidates):
        try:
            proc = subprocess.run(
                ["git", "grep", "-n", "--fixed-strings", "--", name],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode not in (0, 1):
            continue
        found_production_reference = False
        for line in proc.stdout.splitlines():
            path = line.split(":", 1)[0].strip().replace("\\", "/")
            if not path or path in changed_set:
                continue
            if not _is_test_path(path):
                found_production_reference = True
                break
        if not found_production_reference:
            missing.append(name)
    return missing


def _worker_no_progress_timeout_seconds(ctx: RunContext, subtasks: list[dict[str, Any]] | None = None) -> float:
    raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_NO_PROGRESS_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_NO_PROGRESS_TIMEOUT_SECONDS=%r", raw)
    from src.tandem_agents.core.execution.worker import (
        _scaled_async_prompt_timeout_seconds,
        _scaled_prompt_sync_timeout_seconds,
        _worker_terminalize_timeout_seconds,
        _worker_timeout_multiplier,
    )

    prompt_budget = 0.0
    pending = subtasks or [{"write_required": True}]
    for index, subtask in enumerate(pending, start=1):
        write_required = bool(subtask.get("write_required", True))
        timeout_multiplier = _worker_timeout_multiplier(subtask)
        subtask_budget = _scaled_prompt_sync_timeout_seconds(
            ctx.cfg,
            f"worker-{index}",
            write_required,
            timeout_multiplier,
        )
        if write_required:
            # Write-required workers use prompt_sync first, then may retry once
            # through async streaming before returning an engine timeout result.
            subtask_budget += _scaled_async_prompt_timeout_seconds(
                ctx.cfg,
                f"worker-{index}",
                write_required,
                timeout_multiplier,
            )
        prompt_budget = max(prompt_budget, subtask_budget)
    terminalize_budget = _worker_terminalize_timeout_seconds(ctx.cfg)
    return max(1.0, prompt_budget + terminalize_budget + 30.0)


def dispatch_workers(ctx: RunContext) -> None:
    """Execute the pending subtask worker pool and collect results.

    If ``ctx.pending_subtasks`` is empty this is a no-op (results already
    accumulated by ``pre_screen_subtasks`` for the pre-satisfied path).

    Mutates:
        ctx.worker_results     -- extended with results from pending subtasks
        ctx.repo_validation    -- refreshed after worker sync
        ctx.blackboard, ctx.status
    """
    if not ctx.pending_subtasks:
        logger.debug(
            "No pending subtasks; skipping worker dispatch (run_id=%s)", ctx.run_id
        )
        _post_dispatch_validation(ctx)
        return

    from src.tandem_agents.core.engine.engine import effective_tandem_provider
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import set_status, write_blackboard_snapshot, write_status

    worker_provider, worker_model = ctx.cfg.provider_for_role("worker")
    worker_capabilities = {
        "mode": "local-worker-pool",
        "provider": worker_provider,
        "model": worker_model,
        "repository": ctx.repo.get("slug") or ctx.cfg.repository.slug,
        "worktree_mode": "single-host",
    }
    worker_lease_id = str(ctx.status.get("coordination", {}).get("lease_id") or "")

    # Transition status
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="worker_execution",
        phase_role="worker",
        run_status="running",
    )
    _rc._touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="worker_execution",
        ctx=ctx,
    )
    append_event(
        ctx.layout["events"],
        "swarm.spawned",
        ctx.run_id,
        {
            "planned_workers": len(ctx.planned_subtasks),
            "max_parallel": max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1),
            "spawned_workers": len(ctx.pending_subtasks),
        },
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    write_status(ctx.layout["status"], ctx.status)

    # --- Heartbeat thread ---
    active_workers_lock = threading.Lock()
    active_workers: set[str] = set()
    active_worker_started_at: dict[str, float] = {}
    active_worker_started_at_ms: dict[str, int] = {}
    active_worker_worktrees: dict[str, Path] = {}
    active_worker_subtasks: dict[str, dict[str, Any]] = {}
    active_worker_progress_snapshots: dict[str, dict[str, Any]] = {}
    active_worker_snapshot_digests: dict[str, str] = {}
    active_worker_abort_results: dict[str, dict[str, Any]] = {}
    last_progress_event_at = 0.0
    worker_heartbeat_stop = threading.Event()

    def _runaway_diff_max_bytes() -> int:
        raw = str((getattr(ctx.cfg, "env", {}) or {}).get("ACA_WORKER_RUNAWAY_DIFF_MAX_BYTES") or "").strip()
        if raw:
            try:
                return max(1_000, int(raw))
            except ValueError:
                logger.warning("Ignoring invalid ACA_WORKER_RUNAWAY_DIFF_MAX_BYTES=%r", raw)
        return 1_000_000

    def _snapshot_worker_progress_diff(wid: str, worktree: Path) -> dict[str, Any] | None:
        try:
            from src.tandem_agents.core.execution.worker import (  # noqa: PLC0415
                _applyable_working_diff,
                _worktree_changed_files,
                git_working_diff,
            )

            changed_files = _worktree_changed_files(worktree)
            if not changed_files:
                return None
            diff_text = _applyable_working_diff(worktree).strip()
            if not diff_text:
                diff_text = git_working_diff(worktree).strip()
            if not diff_text:
                return None
            diff_bytes = len(diff_text.encode("utf-8", errors="replace"))
            diff_lines = diff_text.count("\n") + 1
            digest = hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest()
            if active_worker_snapshot_digests.get(wid) == digest:
                return active_worker_progress_snapshots.get(wid)
            artifacts_dir = ctx.run_dir / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = artifacts_dir / f"{wid}.progress-partial-worker-diff.patch"
            status_rows = "\n".join(f"- {path}" for path in changed_files)
            max_bytes = _runaway_diff_max_bytes()
            if diff_bytes > max_bytes:
                excerpt = diff_text[:20_000].rstrip()
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker diff exceeded ACA runaway size guard\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## runaway guard\n\n"
                    f"- diff_bytes: {diff_bytes}\n"
                    f"- diff_lines: {diff_lines}\n"
                    f"- max_bytes: {max_bytes}\n\n"
                    "## clipped git diff excerpt\n\n"
                    f"{excerpt}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_runaway_diff_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "max_bytes": max_bytes,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = ""
                with active_workers_lock:
                    for key, path in active_worker_worktrees.items():
                        if key == wid:
                            name = path.name
                            subtask_id = name.split("--", 1)[1] if "--" in name else ""
                            break
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_RUNAWAY_DIFF",
                        "blocker_kind": "worker_runaway_diff",
                        "output_excerpt": (
                            f"Worker diff exceeded ACA runaway guard ({diff_bytes} bytes across "
                            f"{diff_lines} lines; max {max_bytes}). ACA preserved a clipped summary "
                            "and abandoned this worker instead of writing a giant patch artifact."
                        ),
                        "recovery_action": (
                            "Retry with a smaller scoped prompt and require the worker to inspect diff stats "
                            "before continuing after large generated edits."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                    }
                append_event(
                    ctx.layout["events"],
                    "worker.runaway_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            with active_workers_lock:
                subtask = dict(active_worker_subtasks.get(wid) or {})
                started_at = float(active_worker_started_at.get(wid) or time.monotonic())
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            testless_abort_seconds = _worker_testless_diff_abort_seconds(ctx)
            comment_only_abort_seconds = _worker_comment_only_diff_abort_seconds(ctx)
            required_test_files = _subtask_required_test_files(subtask)
            if (
                testless_abort_seconds > 0
                and elapsed_seconds >= testless_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and not any(_is_test_path(path) for path in changed_files)
            ):
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker drifted off required regression/test coverage\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## off-track guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- abort_seconds: {testless_abort_seconds:.1f}\n"
                    f"- required_test_files: {required_test_files}\n"
                    "- reason: subtask requires test/regression coverage but the worker has only changed non-test files\n\n"
                    f"## git diff --binary\n\n{diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_off_track_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "abort_seconds": testless_abort_seconds,
                    "required_test_files": required_test_files,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_OFF_TRACK_TESTLESS_DIFF",
                        "blocker_kind": "worker_off_track",
                        "output_excerpt": (
                            "Worker drifted off the required regression/test coverage path: "
                            f"after {elapsed_seconds:.0f}s it had changed only non-test files "
                            f"while required test files were {', '.join(required_test_files)}."
                        ),
                        "recovery_action": (
                            "Retry from a clean checkout. First read and edit the required test file, "
                            "then make any minimal production change needed for those assertions."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                    }
                append_event(
                    ctx.layout["events"],
                    "worker.off_track_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            unproductive_reason = ""
            if _diff_has_unproductive_marker(diff_text):
                unproductive_reason = "worker diff contains an explicit placeholder/blocker marker"
            elif _diff_has_placeholder_noop_test(diff_text):
                unproductive_reason = "worker diff adds an explicit placeholder/no-op test"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _diff_is_comment_only(diff_text)
            ):
                unproductive_reason = "worker diff is comment-only after the comment-only guard budget"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and (missing_calls := _diff_missing_production_function_calls(worktree, diff_text, changed_files))
            ):
                unproductive_reason = (
                    "worker test-only diff calls missing production helper(s): "
                    + ", ".join(missing_calls)
                )
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_is_local_string_oracle_test(diff_text)
            ):
                unproductive_reason = "worker test-only diff asserts hardcoded local strings instead of production behavior"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_has_tautological_boolean_assertion(diff_text)
            ):
                unproductive_reason = "worker diff contains only tautological boolean assertions"
            elif (
                comment_only_abort_seconds > 0
                and elapsed_seconds >= comment_only_abort_seconds
                and _subtask_requires_test_changes(subtask)
                and changed_files
                and all(_is_test_path(path) for path in changed_files)
                and _diff_is_string_only_change(diff_text)
            ):
                unproductive_reason = "worker diff changes only string wording in tests"
            if unproductive_reason:
                artifact_path.write_text(
                    "# Partial worker diff captured during worker progress heartbeat\n"
                    "# Reason: active worker produced an unproductive placeholder/comment-only diff\n\n"
                    f"## changed files\n\n{status_rows}\n\n"
                    "## unproductive diff guard\n\n"
                    f"- elapsed_seconds: {elapsed_seconds:.1f}\n"
                    f"- comment_only_abort_seconds: {comment_only_abort_seconds:.1f}\n"
                    f"- reason: {unproductive_reason}\n\n"
                    f"## git diff --binary\n\n{diff_text}\n",
                    encoding="utf-8",
                )
                snapshot = {
                    "worker_id": wid,
                    "partial_diff_artifact": str(artifact_path),
                    "changed_files": list(changed_files),
                    "partial_diff_state": "preserved_not_accepted",
                    "source": "worker_unproductive_diff_guard",
                    "diff_bytes": diff_bytes,
                    "diff_lines": diff_lines,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "comment_only_abort_seconds": comment_only_abort_seconds,
                    "reason": unproductive_reason,
                }
                active_worker_snapshot_digests[wid] = digest
                active_worker_progress_snapshots[wid] = snapshot
                subtask_id = str(subtask.get("id") or "").strip()
                with active_workers_lock:
                    active_worker_abort_results[wid] = {
                        "worker_id": wid,
                        "subtask_id": subtask_id,
                        "status": "failed",
                        "returncode": 1,
                        "partial_diff_state": "preserved_not_accepted",
                        "partial_diff_artifact": str(artifact_path),
                        "artifacts": {"partial_diff": str(artifact_path)},
                        "changed_files": list(changed_files),
                        "failure_reason": "WORKER_UNPRODUCTIVE_DIFF",
                        "blocker_kind": "worker_unproductive_diff",
                        "output_excerpt": (
                            "Worker produced an unproductive partial diff: "
                            f"{unproductive_reason}. ACA preserved the patch and abandoned "
                            "this worker instead of waiting for another engine timeout."
                        ),
                        "recovery_action": (
                            "Retry from a clean checkout with a smaller repair prompt. Require a real "
                            "production-path assertion or implementation change before any comments or blockers."
                        ),
                        "write_required": True,
                        "verified_existing": False,
                    }
                append_event(
                    ctx.layout["events"],
                    "worker.unproductive_diff_detected",
                    ctx.run_id,
                    snapshot,
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                return snapshot
            artifact_path.write_text(
                "# Partial worker diff captured during worker progress heartbeat\n"
                "# Reason: active worker had filesystem changes before terminal result\n\n"
                f"## changed files\n\n{status_rows}\n\n"
                f"## git diff --binary\n\n{diff_text}\n",
                encoding="utf-8",
            )
            snapshot = {
                "worker_id": wid,
                "partial_diff_artifact": str(artifact_path),
                "changed_files": list(changed_files),
                "partial_diff_state": "preserved_not_accepted",
                "source": "worker_progress_snapshot",
                "diff_bytes": diff_bytes,
                "diff_lines": diff_lines,
            }
            active_worker_snapshot_digests[wid] = digest
            active_worker_progress_snapshots[wid] = snapshot
            append_event(
                ctx.layout["events"],
                "worker.progress_partial_diff_snapshot",
                ctx.run_id,
                snapshot,
                task_id=ctx.task.get("task_id"),
                role="worker",
                repo={"path": ctx.repo.get("path")},
            )
            return snapshot
        except Exception:
            logger.debug("Failed to snapshot worker progress diff for %s", wid, exc_info=True)
            return None

    def _attach_progress_snapshot_to_failed_result(result: dict[str, Any]) -> None:
        if result.get("returncode") == 0 or result.get("partial_diff_artifact"):
            return
        blocker_kind = str(result.get("blocker_kind") or "").strip()
        if blocker_kind not in {"engine_prompt_timeout", "engine_tool_loop_stalled"}:
            return
        wid = str(result.get("worker_id") or "").strip()
        snapshot = active_worker_progress_snapshots.get(wid)
        if not snapshot:
            return
        result.setdefault("artifacts", {})["partial_diff"] = snapshot["partial_diff_artifact"]
        result["partial_diff_artifact"] = snapshot["partial_diff_artifact"]
        result["changed_files"] = list(snapshot.get("changed_files") or [])
        result["progress_partial_diff_recovered"] = True
        result["engine_blocker_kind"] = blocker_kind
        result["blocker_kind"] = "worker_incomplete_diff"
        result["recovery_action"] = (
            "ACA captured a progress-time partial diff before the engine timeout; "
            "inspect that artifact and retry from a clean checkout."
        )
        append_event(
            ctx.layout["events"],
            "worker.partial_diff_preserved",
            ctx.run_id,
            {
                "worker_id": wid,
                "subtask_id": str(result.get("subtask_id") or "").strip(),
                "partial_diff_state": "preserved_not_accepted",
                "partial_diff_artifact": snapshot["partial_diff_artifact"],
                "changed_files": list(snapshot.get("changed_files") or []),
                "failure_reason": result.get("failure_reason"),
                "blocker_kind": result.get("blocker_kind"),
                "recovery_action": result.get("recovery_action"),
                "source": "worker_progress_snapshot",
            },
            task_id=ctx.task.get("task_id"),
            role="worker",
            repo={"path": ctx.repo.get("path")},
        )

    def _terminal_worker_events_since(started_at_ms: dict[str, int]) -> set[str]:
        if not started_at_ms:
            return set()
        terminal_ids: set[str] = set()
        try:
            for raw_line in ctx.layout["events"].read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                event_type = str(event.get("type") or "")
                if event_type not in {"worker.completed", "worker.failed"}:
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                worker_id = str(payload.get("worker_id") or "").strip()
                if not worker_id or worker_id not in started_at_ms:
                    continue
                try:
                    event_at_ms = int(event.get("timestamp_ms") or 0)
                except (TypeError, ValueError):
                    event_at_ms = 0
                if event_at_ms >= int(started_at_ms.get(worker_id) or 0):
                    terminal_ids.add(worker_id)
        except Exception:
            logger.debug("Failed to scan terminal worker events for heartbeat pruning", exc_info=True)
        return terminal_ids

    def _heartbeat_local_workers() -> None:
        sleep_s = max(1.0, float(ctx.cfg.coordination.heartbeat_interval_seconds or 1) / 2.0)
        while not worker_heartbeat_stop.wait(sleep_s):
            _rc._touch_coordination(
                ctx.coordination,
                run_id=ctx.run_id,
                lease_id=ctx.lease_id,
                lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                status="running",
                phase="worker_execution",
                ctx=ctx,
            )
            with active_workers_lock:
                ids = list(active_workers)
                started_at = dict(active_worker_started_at)
                started_at_ms = dict(active_worker_started_at_ms)
                worktrees = dict(active_worker_worktrees)
            terminal_ids = _terminal_worker_events_since(started_at_ms)
            if terminal_ids:
                with active_workers_lock:
                    for wid in terminal_ids:
                        active_workers.discard(wid)
                        active_worker_started_at.pop(wid, None)
                        active_worker_started_at_ms.pop(wid, None)
                        active_worker_worktrees.pop(wid, None)
                        active_worker_subtasks.pop(wid, None)
                    ids = [wid for wid in ids if wid not in terminal_ids]
                    started_at = {wid: value for wid, value in started_at.items() if wid not in terminal_ids}
                    worktrees = {wid: value for wid, value in worktrees.items() if wid not in terminal_ids}
            for wid in ids:
                try:
                    ctx.coordination.heartbeat_worker(
                        wid,
                        host_id=ctx.claim_identity["host_id"],
                        role="worker",
                        status="busy",
                        capabilities=worker_capabilities,
                        current_run_id=ctx.run_id,
                        current_lease_id=worker_lease_id,
                    )
                except Exception:
                    logger.debug("Heartbeat failed for worker %s", wid, exc_info=True)
                worktree = worktrees.get(wid)
                if worktree:
                    _snapshot_worker_progress_diff(wid, worktree)
            now = time.monotonic()
            progress_interval = max(30.0, float(ctx.cfg.coordination.heartbeat_interval_seconds or 1) * 2.0)
            nonlocal last_progress_event_at
            if ids and now - last_progress_event_at >= progress_interval:
                last_progress_event_at = now
                elapsed_by_worker = {
                    wid: round(max(0.0, now - float(started_at.get(wid, now))), 1)
                    for wid in ids
                }
                detail = ", ".join(
                    f"{wid} running for {elapsed_by_worker[wid]:.0f}s" for wid in ids[:3]
                )
                phase = ctx.status.get("phase") if isinstance(ctx.status.get("phase"), dict) else {}
                phase["detail"] = detail or "worker still running"
                ctx.status["phase"] = phase
                append_event(
                    ctx.layout["events"],
                    "worker.progress",
                    ctx.run_id,
                    {
                        "active_workers": ids,
                        "elapsed_seconds_by_worker": elapsed_by_worker,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                write_status(ctx.layout["status"], ctx.status)

    def _on_result(result: dict[str, Any]) -> None:
        wid = str(result.get("worker_id") or "").strip()
        subtask_id = str(result.get("subtask_id") or "").strip()
        _attach_progress_snapshot_to_failed_result(result)
        _rc._record_worker_result(ctx.blackboard, ctx.worker_results, result)
        for item in ctx.blackboard["subtasks"]:
            if item.get("id") == subtask_id:
                item["status"] = result.get("status") or "failed"
                break
        ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        write_status(ctx.layout["status"], ctx.status)
        if wid:
            ctx.coordination.register_worker(
                worker_id=wid,
                host_id=ctx.claim_identity["host_id"],
                role="worker",
                status="idle",
                capabilities=worker_capabilities,
                current_run_id=None,
                current_lease_id=None,
            )
            with active_workers_lock:
                active_workers.discard(wid)
                active_worker_started_at.pop(wid, None)
                active_worker_started_at_ms.pop(wid, None)
                active_worker_worktrees.pop(wid, None)
                active_worker_subtasks.pop(wid, None)
                active_worker_progress_snapshots.pop(wid, None)
                active_worker_snapshot_digests.pop(wid, None)
                active_worker_abort_results.pop(wid, None)

    def _on_start(wid: str, subtask: dict[str, Any]) -> None:
        wid = str(wid or "").strip()
        if not wid:
            return
        ctx.coordination.register_worker(
            worker_id=wid,
            host_id=ctx.claim_identity["host_id"],
            role="worker",
            status="busy",
            capabilities=worker_capabilities,
            current_run_id=ctx.run_id,
            current_lease_id=worker_lease_id,
        )
        with active_workers_lock:
            active_workers.add(wid)
            active_worker_started_at[wid] = time.monotonic()
            active_worker_started_at_ms[wid] = int(time.time() * 1000)
            subtask_id = str(subtask.get("id") or "").strip()
            if subtask_id:
                active_worker_worktrees[wid] = ctx.run_dir / "worktrees" / f"{wid}--{subtask_id}"
                active_worker_subtasks[wid] = dict(subtask)

    def _abort_result(index: int, subtask: dict[str, Any], wid: str) -> dict[str, Any] | None:
        with active_workers_lock:
            result = active_worker_abort_results.get(str(wid or ""))
        if not result:
            return None
        result = dict(result)
        result.setdefault("subtask_index", index)
        result.setdefault("subtask_id", subtask.get("id"))
        result.setdefault("title", subtask.get("title"))
        return result

    heartbeat_thread = threading.Thread(target=_heartbeat_local_workers, daemon=True)
    heartbeat_thread.start()

    try:
        logger.info(
            "Dispatching %d worker(s) (run_id=%s)", len(ctx.pending_subtasks), ctx.run_id
        )
        new_results = _rc._execute_local_worker_pool(
            ctx.cfg,
            ctx.run_id,
            ctx.repo_path,
            ctx.run_dir,
            ctx.task,
            ctx.pending_subtasks,
            max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1),
            on_start=_on_start,
            on_result=_on_result,
            abort_result=_abort_result,
            worker_timeout_seconds=_worker_no_progress_timeout_seconds(ctx, ctx.pending_subtasks),
        )
        # Merge any results that bypassed _on_result
        for r in new_results:
            if not any(
                existing.get("worker_id") == r.get("worker_id")
                and existing.get("subtask_id") == r.get("subtask_id")
                for existing in ctx.worker_results
            ):
                ctx.worker_results.append(r)
    finally:
        worker_heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)
        with active_workers_lock:
            lingering = list(active_workers)
            active_workers.clear()
        for wid in lingering:
            try:
                ctx.coordination.register_worker(
                    worker_id=wid,
                    host_id=ctx.claim_identity["host_id"],
                    role="worker",
                    status="idle",
                    capabilities=worker_capabilities,
                    current_run_id=None,
                    current_lease_id=None,
                )
            except Exception:
                logger.debug("Failed to unregister lingering worker %s", wid, exc_info=True)

    _apply_tolerated_failures(ctx)
    ctx.status["metrics"].update(_rc._worker_result_metrics(ctx.worker_results))
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    write_status(ctx.layout["status"], ctx.status)

    _post_dispatch_validation(ctx)


def _apply_tolerated_failures(ctx: RunContext) -> None:
    """Upgrade 'failed' results to 'tolerated_failure' when target files are present post-sync."""
    from src.tandem_agents.core.repository.repo_truth import subtask_satisfied
    from src.tandem_agents.core.execution import runner_core as _rc

    task_source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
    if isinstance(task_source, dict) and str(task_source.get("type") or "").strip() == "github_project":
        return
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        return

    for result in ctx.worker_results:
        if result.get("status") != "failed":
            continue
        matching = next(
            (s for s in ctx.planned_subtasks if s["id"] == result.get("subtask_id")),
            None,
        )
        failure_reason = str(result.get("failure_reason") or "").upper()
        blocker_kind = str(result.get("blocker_kind") or "").lower()
        if (
            failure_reason.startswith("ENGINE_")
            or failure_reason.startswith("ENGINE_ERROR:")
            or blocker_kind.startswith("engine_")
            or blocker_kind in _TERMINAL_WORKER_BLOCKER_KINDS
            or (matching and (matching.get("pr_candidate_context") or matching.get("pr_candidate_refs")))
        ):
            continue
        if matching and subtask_satisfied(ctx.repo_path, matching):
            result["status"] = "tolerated_failure"
            result["verified_existing"] = True
            for item in ctx.blackboard["subtasks"]:
                if item.get("id") == result["subtask_id"]:
                    item["status"] = "tolerated_failure"
                    break
            _rc._append_blackboard_note(
                ctx.blackboard,
                f"Tolerated noisy worker `{result['worker_id']}` because its target files were present after sync.",
            )


def _post_dispatch_validation(ctx: RunContext) -> None:
    """Refresh repo_validation and coding_run_contract after worker execution."""
    from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    ctx.expected_repo_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    changed_files: list[str] = _rc._collect_worker_changed_files(ctx.worker_results)
    if _rc._task_mentions_external_pr_candidates(ctx.task):
        if changed_files:
            ctx.expected_repo_files = changed_files
    else:
        ctx.expected_repo_files = _rc._sticky_expected_repo_files(
            ctx.blackboard,
            ctx.expected_repo_files,
        )
        ctx.expected_repo_files = _rc._validation_expected_repo_files(
            ctx.repo_path,
            ctx.expected_repo_files,
            changed_files,
        )
        ctx.blackboard["expected_repo_files"] = ctx.expected_repo_files
    ctx.repo_validation = _rc._deterministic_repo_validation(ctx.repo_path, ctx.expected_repo_files)
    if changed_files:
        unexpected_files = _rc._pr_candidate_unexpected_changed_files(ctx.planned_subtasks, changed_files)
        if unexpected_files:
            ctx.repo_validation = dict(ctx.repo_validation)
            ctx.repo_validation["unexpected_files"] = unexpected_files
            ctx.repo_validation["ok"] = False
    coding_run_contract = build_coding_run_contract(
        run_id=ctx.run_id,
        task=ctx.task,
        repo_path=ctx.repo_path,
        branch_name=ctx.branch_name,
        expected_repo_files=ctx.expected_repo_files,
    )
    ctx.blackboard["repo_validation"] = ctx.repo_validation
    _rc._record_coding_run_contract(ctx.blackboard, coding_run_contract)

    if ctx.repo_validation.get("ok"):
        _rc._append_blackboard_note(
            ctx.blackboard, "Deterministic repo validation passed for expected files."
        )
    else:
        blocker = _rc._repo_validation_blocker_message(ctx.repo_validation)
        _rc._append_blackboard_note(
            ctx.blackboard,
            f"Deterministic repo validation found issues: {blocker or 'unknown issue'}",
        )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
