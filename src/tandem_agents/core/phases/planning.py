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

import logging
from typing import Any

from src.tandem_agents.core.phases.context import RunContext
from src.tandem_agents.core.task_contract import task_contract_payload

logger = logging.getLogger("aca.phases.planning")

_MANAGER_PLAN_REQUIRED_KEYS = {"summary", "subtasks", "risks", "tests"}


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
            if rel_path
        )
    )


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
    # Dispatch concurrency is limited later. When swarm is disabled, preserve
    # manager subtasks and run them serially instead of compacting a broad plan
    # into one prompt that can exhaust the engine iteration/timeout budget.
    planning_subtask_limit = max(1, ctx.cfg.swarm.max_workers if ctx.cfg.swarm.enabled else 1)
    return _rc._prepare_subtasks_with_discovery(
        ctx.task,
        ctx.manager_plan,
        Path(ctx.repo.get("path") or "."),
        planning_subtask_limit,
        merge_manager_subtasks=ctx.cfg.swarm.enabled,
    )


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
        if subtask is None or subtask.get("carry_forward_patch"):
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
        changed_file_note = (
            " The saved diff touched these files; read and finish them before adding new scope: "
            + ", ".join(changed_files)
            + "."
            if changed_files
            else ""
        )
        blocker_note = (
            "\nRecovered partial-diff blocker/context:\n" + worker_output_excerpt
            if worker_output_excerpt and should_reapply_patch
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
            summary_note = f" Failure summary: {rejected_failure_summary}." if rejected_failure_summary else ""
            carry_note = (
                "ACA rejected the preserved partial worker diff for this retry because the recovered notes describe "
                "incomplete, unverified, helper-only, self-referential, or test-only coverage. Start from the clean "
                "target files, remove that approach if present, and add coverage that calls existing production code "
                "instead."
                f"{rejected_changed_file_note}{summary_note}"
            )
        subtask["scope_note"] = f"{existing_scope_note}\n{carry_note}".strip()


def _partial_diff_rejected_failure_summary(worker_output_excerpt: str) -> str:
    text = worker_output_excerpt.lower()
    reasons: list[str] = []
    if "engine_prompt_timeout" in text:
        reasons.append("the worker timed out before a terminal response")
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
    if "not treated as a completed worker result" in text:
        reasons.append("ACA did not accept the partial as completed work")
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
        "not treated as a completed worker result",
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
            if "/workspace/" in text and "partial" in lowered and "patch" in lowered:
                if replacement not in sanitized:
                    sanitized.append(replacement)
                continue
            sanitized.append(entry)
        subtask["acceptance_criteria"] = sanitized


def _repair_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    carried = [subtask for subtask in subtasks if subtask.get("carry_forward_patch")]
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
        if rel_path
    ]
    if changed_files:
        previous_files = sorted(_subtask_declared_files(chosen).difference(changed_files))
        chosen["files"] = list(dict.fromkeys(changed_files))
        chosen["target_files"] = list(dict.fromkeys(changed_files))
        if previous_files:
            chosen["repair_deferred_files"] = previous_files
    existing_scope_note = str(chosen.get("scope_note") or "").strip()
    narrow_note = (
        "ACA narrowed this extra repair attempt to the carried partial-diff subtask so the worker "
        "finishes the preserved files before the manager can expand into new swarm slices."
    )
    if changed_files:
        narrow_note += " Active repair targets are limited to the changed files from the preserved patch: " + ", ".join(changed_files) + "."
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


def _completed_repair_subtask_ids(ctx: RunContext) -> set[str]:
    repair = ctx.blackboard.get("repair") if isinstance(ctx.blackboard, dict) else {}
    raw_ids = repair.get("completed_subtask_ids") if isinstance(repair, dict) else []
    if not isinstance(raw_ids, list):
        return set()
    return {str(item).strip() for item in raw_ids if str(item).strip()}


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

    append_event(
        ctx.layout["events"],
        "manager.started",
        ctx.run_id,
        {"role": "manager", "repo_context": dict(ctx.blackboard["repo_context"])},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )

    logger.info("Running manager prompt (run_id=%s)", ctx.run_id)
    with _rc._coordination_heartbeat(ctx, phase="planning"):
        manager_result = stream_tandem_prompt(
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
        ctx.status = set_status(
            ctx.status,
            ctx.layout,
            phase="planning",
            phase_detail=invalid_plan_reason,
            run_status="blocked",
            blocker=(True, "manager_invalid_plan", invalid_plan_reason, "manager"),
            run_completed=True,
        )
        append_event(
            ctx.layout["events"],
            "manager.invalid_plan",
            ctx.run_id,
            {"reason": invalid_plan_reason, "stdout_excerpt": excerpt},
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

    ctx.planned_subtasks = subtasks
    ctx.pending_subtasks = []
    current_expected_files = _rc._collect_expected_repo_files(ctx.planned_subtasks)
    sticky_expected_files = _rc._sticky_expected_repo_files(ctx.blackboard, current_expected_files)
    sticky_missing_from_plan = [path for path in sticky_expected_files if path not in current_expected_files]
    if sticky_missing_from_plan and ctx.planned_subtasks:
        first = ctx.planned_subtasks[0]
        extra_partial_repair = _extra_partial_diff_repair_active(ctx) and bool(first.get("carry_forward_patch"))
        if not extra_partial_repair:
            for key in ("files", "target_files"):
                values = [str(entry).strip() for entry in (first.get(key) or []) if str(entry).strip()]
                for path in sticky_missing_from_plan:
                    if path not in values:
                        values.append(path)
                first[key] = values
        existing_scope_note = str(first.get("scope_note") or "").strip()
        if extra_partial_repair:
            sticky_note = (
                "ACA deferred these expected files from an earlier retry attempt while finishing the "
                "preserved partial diff: "
                + ", ".join(sticky_missing_from_plan)
                + "."
            )
            deferred = [str(entry).strip() for entry in (first.get("repair_deferred_files") or []) if str(entry).strip()]
            for path in sticky_missing_from_plan:
                if path not in deferred:
                    deferred.append(path)
            first["repair_deferred_files"] = deferred
        else:
            sticky_note = (
                "ACA kept these expected files from an earlier retry attempt because later manager plans "
                "must not narrow the run contract: "
                + ", ".join(sticky_missing_from_plan)
                + "."
            )
        first["scope_note"] = f"{existing_scope_note}\n{sticky_note}".strip()
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
        subtask["pre_satisfied"] = (
            True
            if carried_forward_success
            else (False if force_worker_execution else subtask_satisfied(repo_path, subtask))
        )
        subtask["write_required"] = not subtask["pre_satisfied"]

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

    ctx.expected_repo_files = _rc._sticky_expected_repo_files(
        ctx.blackboard,
        _rc._collect_expected_repo_files(ctx.planned_subtasks),
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
