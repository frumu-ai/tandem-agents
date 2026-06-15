from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from pathlib import Path
from typing import Any, Callable

from src.tandem_agents.core.repository.board import card_to_task, claim_card, move_card, save_board, select_card
from src.tandem_agents.config.config_types import ResolvedConfig
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.coordination.coordination import CoordinationStore, default_host_id, default_worker_id
from src.tandem_agents.core.shutdown import ShutdownHandler
from src.tandem_agents.core.engine.coder_backend import (
    build_coder_summary,
    coder_backend_mode,
    coder_workflow_supported,
    execute_coder_run,
)
from src.tandem_agents.core.engine.engine import (
    checkout_run_branch,
    commit_repository_changes,
    engine_env,
    engine_session_provider_model,
    engine_health,
    engine_visible_path,
    ensure_engine,
    git_diff_stat,
    list_engine_permissions,
    push_repository_changes,
    reply_engine_permission,
    resolve_repository,
    task_run_branch_name,
    write_provider_override_config,
)
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.core.integrations.github_mcp import (
    add_issue_comment,
    build_issue_comment_body,
    ensure_github_mcp_connected,
    ensure_github_mcp_disconnected,
    github_project_status_name_for_outcome,
    github_project_status_name_for_task_state,
    get_mcp_server,
    get_pull_request,
    github_mcp_scope,
    github_remote_sync_mode,
    list_pull_requests,
    update_project_item_status,
)
from src.tandem_agents.core.integrations.linear_mcp import (
    build_linear_comment_body,
    ensure_linear_mcp_connected,
    linear_add_comment,
    linear_mcp_scope,
    linear_mcp_server_name,
    linear_remote_sync_mode,
    linear_status_name_for_outcome,
    linear_status_name_for_task_state,
    linear_update_issue,
)
from src.tandem_agents.core.repository.repository import _git_repo_args, fetch_pr_refs, repository_binding_issues
from src.tandem_agents.core.task_contract import task_contract_payload
from src.tandem_agents.core.scheduling.outbox_dispatcher import dispatch_outbox_tick
from src.tandem_agents.core.scheduling.coder_supervisor import apply_coder_result
from src.tandem_agents.core.verification.review_policy import evaluate_review_policy
from src.tandem_agents.core.verification.verification_policy import evaluate_verification_policy, review_blocker_message, test_blocker_message
from src.tandem_agents.core.engine.prompts import build_integration_prompt, build_manager_prompt, build_qa_prompt, build_review_prompt, build_test_prompt, derive_subtasks
from src.tandem_agents.core.repository.repo_truth import (
    collect_expected_repo_files,
    deterministic_repo_validation,
    discover_repo_files,
    extract_command_checks,
    file_is_readable,
    repo_context_summary,
    repo_validation_blocker_message,
    subtask_satisfied,
)
from src.tandem_agents.core.execution.run_lifecycle import (
    block_run,
    build_provider_config_dict,
    build_swarm_config_dict,
    make_run_result,
)
from src.tandem_agents.core.external_actions.github_pr import (
    default_action_plan,
    enqueue_approvals_for_plan,
    fetch_pr_contexts,
)
from src.tandem_agents.core.phases.engine_check import (
    check_engine_at_startup,
    check_engine_health,
    resolve_repo_after_checkout,
)
from src.tandem_agents.core.phases.planning import (
    pre_screen_subtasks,
    run_manager_prompt,
)
from src.tandem_agents.core.phases.review_verify import run_review_and_test
from src.tandem_agents.core.phases.finalize import finalize_completed_run
from src.tandem_agents.core.phases.github_sync import (
    connect_for_intake,
    disconnect_for_coding,
    sync_claim_status,
    finalize_sync,
)
from src.tandem_agents.core.phases.task_intake import run_task_intake
from src.tandem_agents.core.phases.worker_dispatch import dispatch_workers
from src.tandem_agents.core.phases.repair import (
    RepairDecision,
    build_retry_feedback,
    check_no_diff,
    check_no_verifiable_proof,
)
from src.tandem_agents.core.phases.context import RunContext as _PhaseRunContext
from src.tandem_agents.runtime.run_output import build_blocked_summary, build_completed_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot, write_diff_snapshot
from src.tandem_agents.runtime.artifact_store import configure_artifact_store_root
from src.tandem_agents.runtime.runstate import append_event, ensure_layout, initial_blackboard, initial_status, load_status, new_run_id, save_blackboard, write_status
from src.tandem_agents.runtime.task_sources import invalidate_cached_github_project_board_snapshot, normalize_task
from src.tandem_agents.utils.utils import slugify
from src.tandem_agents.core.execution.worker import run_worker_subtask, stream_tandem_prompt, sync_worker_artifacts
from src.tandem_agents.core.engine.tandem_client_sdk import (
    sdk_agent_teams_list_approvals,
    sdk_agent_teams_approve_spawn,
)

logger = logging.getLogger("aca.runner_core")

_RETRYABLE_WORKER_BLOCKER_KINDS = {
    "worker_incomplete_diff",
    "worker_no_progress",
    "worker_corrupt_diff",
    "worker_no_diff",
    "engine_tool_loop_stalled",
    "engine_tool_loop_stalled_no_diff",
    "engine_prompt_timeout",
}

_EXPLICIT_REPO_PATH_PREFIXES = (
    ".github/",
    "apps/",
    "crates/",
    "docs/",
    "packages/",
    "scripts/",
    "src/",
    "tests/",
)
_EXPLICIT_REPO_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.@+-])((?:\.github|apps|crates|docs|packages|scripts|src|tests)"
    r"(?:/[A-Za-z0-9_.@+-]+)+/?)"
)
_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_:]*)`|\b([A-Za-z_][A-Za-z0-9_]*::[A-Za-z0-9_:]+|[A-Za-z_][A-Za-z0-9_]{4,})\b")


def _extract_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    fence = re.search(r"```json\s*(.*?)\s*```", text, re.S | re.I)
    if fence:
        candidates.append(fence.group(1).strip())
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1].strip())
    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            logger.debug("Failed to parse candidate JSON in _extract_json", exc_info=True)
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _wait_for_engine(cfg: ResolvedConfig, timeout: float = 90.0, poll_interval: float = 5.0) -> None:
    """Block until the tandem engine is healthy, or raise after timeout."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            engine_health(cfg, timeout=5.0)
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(poll_interval)
    raise RuntimeError(f"Tandem engine did not recover within {timeout}s: {last_exc}")


def _append_blackboard_note(blackboard: dict[str, Any], message: str) -> None:
    blackboard.setdefault("notes", []).append(message)


def _task_mentions_external_pr_candidates(task: dict[str, Any] | None) -> bool:
    if not isinstance(task, dict):
        return False
    text = "\n".join(
        [
            str(task.get("title") or ""),
            str(task.get("description") or task.get("raw_issue_body") or ""),
            "\n".join(str(entry or "") for entry in _as_list(task.get("acceptance_criteria"))),
        ]
    ).lower()
    if not re.search(r"(?:^|[\s(])#\d+\b", text):
        return False
    return any(
        marker in text
        for marker in (
            " pr",
            "prs",
            "pull request",
            "pull requests",
            "cherry-pick",
            "merge",
            "close",
            "branch",
        )
    )


def _task_scope_text(task: dict[str, Any] | None) -> str:
    if not isinstance(task, dict):
        return ""
    contract = task_contract_payload(task)
    parts = [
        task.get("title"),
        task.get("description"),
        task.get("raw_issue_body"),
        task.get("body"),
        contract.get("local_goal"),
        contract.get("notes_for_agent"),
    ]
    for key in ("acceptance_criteria", "deliverables", "in_scope", "verification_commands"):
        parts.extend(_as_list(task.get(key)))
        parts.extend(_as_list(contract.get(key)))
    return "\n".join(str(part or "") for part in parts if str(part or "").strip())


def _repo_path_mentions_from_task(task: dict[str, Any] | None) -> list[str]:
    text = _task_scope_text(task).replace("\\", "/")
    candidates: list[str] = []
    for match in _EXPLICIT_REPO_PATH_RE.finditer(text):
        candidate = match.group(1).strip().strip("`'\".,;:)(")
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate and candidate.startswith(_EXPLICIT_REPO_PATH_PREFIXES) and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _symbol_mentions_from_task(task: dict[str, Any] | None) -> list[str]:
    text = _task_scope_text(task)
    symbols: list[str] = []
    noisy = {
        "acceptance",
        "criteria",
        "deliverables",
        "linear",
        "project",
        "repository",
        "tandem",
    }
    for match in _SYMBOL_RE.finditer(text):
        symbol = (match.group(1) or match.group(2) or "").strip("`")
        if not symbol:
            continue
        parts = [part for part in symbol.split("::") if part]
        for part in [symbol, *parts]:
            lower = part.lower()
            if len(part) < 5 or lower in noisy:
                continue
            if part not in symbols:
                symbols.append(part)
    return symbols


def _candidate_source_files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    allowed_suffixes = {
        ".rs",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".py",
        ".go",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
    }
    files: list[Path] = []
    for candidate in path.rglob("*"):
        if not candidate.is_file():
            continue
        rel = candidate.relative_to(path).as_posix()
        if "/target/" in f"/{rel}/" or "/node_modules/" in f"/{rel}/":
            continue
        if candidate.suffix.lower() in allowed_suffixes:
            files.append(candidate)
    return files


def _explicit_file_score(path: Path, repo_path: Path, symbols: list[str], scope_text: str, original_index: int) -> tuple[int, int, str]:
    try:
        rel = path.relative_to(repo_path).as_posix()
    except ValueError:
        rel = path.as_posix()
    lower_rel = rel.lower()
    lower_scope = scope_text.lower()
    score = 0
    if lower_rel.endswith((".rs", ".ts", ".tsx", ".py", ".js", ".mjs")):
        score += 8
    if "/tests/" in f"/{lower_rel}/" or re.search(r"(?:^|/)(?:[^/]*test[^/]*)\.", lower_rel):
        score += 12 if any(word in lower_scope for word in ("test", "tests", "suite", "coverage")) else 2
    if lower_rel.endswith("/lib.rs") or lower_rel.endswith("/mod.rs"):
        score += 8
    for token in re.findall(r"[a-z0-9]+", lower_scope):
        if len(token) >= 5 and token in lower_rel:
            score += 4
    text = ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    for symbol in symbols:
        parts = [symbol, *symbol.split("::")]
        if any(part and part in text for part in parts):
            score += 28
        if any(part and part.lower() in lower_rel for part in parts):
            score += 18
    if "approval_classifier" in lower_scope and "approval_classifier" in lower_rel:
        score += 36
    if "path sandbox" in lower_scope and "builtin_tools" in lower_rel:
        score += 24
    if "registry resolution" in lower_scope and lower_rel.endswith("/lib.rs"):
        score += 20
    if lower_rel.startswith("docs/internal/"):
        score -= 100
    if lower_rel.endswith(("Cargo.lock", "package-lock.json", "pnpm-lock.yaml")):
        score -= 50
    return (-score, original_index, lower_rel)


def _explicit_task_target_files(repo_path: Path, task: dict[str, Any] | None, limit: int = 8) -> list[str]:
    mentions = _repo_path_mentions_from_task(task)
    if not mentions:
        return []
    scope_text = _task_scope_text(task)
    symbols = _symbol_mentions_from_task(task)
    exact_files: list[str] = []
    scored_candidates: list[tuple[tuple[int, int, str], str]] = []
    for mention in mentions:
        candidate = repo_path / mention.rstrip("/")
        if candidate.is_file():
            rel = candidate.relative_to(repo_path).as_posix()
            if rel not in exact_files:
                exact_files.append(rel)
            continue
        for index, file_path in enumerate(_candidate_source_files_under(candidate)):
            try:
                rel = file_path.relative_to(repo_path).as_posix()
            except ValueError:
                continue
            score = _explicit_file_score(file_path, repo_path, symbols, scope_text, index)
            if score[0] < 0:
                scored_candidates.append((score, rel))
    selected = list(exact_files)
    for _, rel in sorted(scored_candidates):
        if rel not in selected:
            selected.append(rel)
        if len(selected) >= limit:
            break
    return selected


def _worker_failure_blocker(worker_results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [
        result
        for result in worker_results
        if str(result.get("status") or "").strip() == "failed"
        or int(result.get("returncode") or 0) != 0
    ]
    first = failed[0] if failed else {}
    kind = str(first.get("blocker_kind") or "").strip() or "worker_failed"
    failure_reason = str(first.get("failure_reason") or "").strip()
    engine = dict(first.get("engine") or {})
    worker_id = str(first.get("worker_id") or "").strip() or "worker"
    if kind == "engine_empty_response":
        message = "Tandem engine returned no assistant transcript after async retry and prompt_sync fallback."
        phase_detail = f"{worker_id} blocked on empty Tandem engine transcript"
        recovery_action = (
            first.get("recovery_action")
            or "Check Tandem engine provider/model routing and persisted engine snapshots, then reset the task to Backlog."
        )
    elif kind == "worker_no_diff":
        message = "Worker inspected the task but produced no repository diff."
        phase_detail = f"{worker_id} produced no filesystem changes"
        recovery_action = (
            first.get("recovery_action")
            or "Inspect the worker log and PR candidate context, then reset the task to Backlog if another attempt is needed."
        )
    elif kind == "worker_no_progress":
        message = failure_reason or "Worker produced no terminal result before the no-progress watchdog fired."
        phase_detail = f"{worker_id} made no terminal progress"
        recovery_action = (
            first.get("recovery_action")
            or "Inspect the worker log and engine run state, then retry after fixing the stalled worker path."
        )
    elif kind == "worker_incomplete_diff":
        message = "Worker produced a partial repository diff but reported remaining blockers."
        phase_detail = f"{worker_id} produced an incomplete diff"
        recovery_action = (
            first.get("recovery_action")
            or "Retry with a narrower repair prompt focused on the unmet acceptance criteria."
        )
    elif kind == "ignored_path_changes":
        ignored = first.get("ignored_files") or []
        ignored_text = f" Ignored files: {', '.join(str(path) for path in ignored)}." if ignored else ""
        message = f"Worker edited only Git-ignored files, so no reviewable repository diff was produced.{ignored_text}"
        phase_detail = f"{worker_id} edited only ignored files"
        recovery_action = (
            first.get("recovery_action")
            or "Move the requested deliverable to tracked repository files, then reset the task to Backlog."
        )
    elif kind == "engine_exception":
        message = failure_reason or "Tandem engine prompt failed with an exception."
        phase_detail = f"{worker_id} hit an engine prompt exception"
        recovery_action = first.get("recovery_action") or "Inspect the worker log and Tandem engine health, then retry."
    elif kind == "engine_provider_auth":
        message = failure_reason or "Tandem engine provider authentication failed."
        phase_detail = f"{worker_id} blocked on Tandem provider authentication"
        recovery_action = (
            first.get("recovery_action")
            or "Repair the Tandem Control Panel provider credentials/model route, then reset the task to Backlog."
        )
    elif kind == "engine_dispatch_failed":
        message = failure_reason or "Tandem engine could not dispatch the provider request."
        phase_detail = f"{worker_id} blocked on Tandem engine dispatch"
        recovery_action = (
            first.get("recovery_action")
            or "Inspect Tandem engine dispatch logs and provider routing, then reset the task to Backlog."
        )
    else:
        message = failure_reason or "One or more workers failed."
        phase_detail = f"{worker_id} failed during worker execution"
        recovery_action = "Inspect the failed worker log, then reset the task to Backlog if another attempt is needed."
    detail_bits = [
        f"worker={worker_id}",
        f"reason={failure_reason or kind}",
    ]
    session_id = str(engine.get("session_id") or "").strip()
    run_id = str(engine.get("run_id") or first.get("engine_run_id") or "").strip()
    fallback_mode = str(engine.get("fallback_mode") or "").strip()
    retry_count = engine.get("retry_count")
    if session_id:
        detail_bits.append(f"session_id={session_id}")
    if run_id:
        detail_bits.append(f"engine_run_id={run_id}")
    if retry_count not in (None, ""):
        detail_bits.append(f"retry_count={retry_count}")
    if fallback_mode:
        detail_bits.append(f"fallback={fallback_mode}")
    return {
        "kind": kind,
        "message": message,
        "detail": "; ".join(detail_bits),
        "phase_detail": phase_detail,
        "recovery_action": str(recovery_action or "").strip(),
        "engine": engine,
        "worker": first,
    }


def _worker_failure_retry_feedback(ctx: "_PhaseRunContext", blocker: dict[str, Any], attempt: int) -> str | None:
    kind = str(blocker.get("kind") or "").strip()
    if kind not in _RETRYABLE_WORKER_BLOCKER_KINDS:
        return None
    changed_files = _collect_worker_changed_files(ctx.worker_results)
    changed_text = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- none"
    worker = blocker.get("worker") if isinstance(blocker.get("worker"), dict) else {}
    patch_path = str(worker.get("partial_diff_artifact") or ((worker.get("artifacts") or {}).get("partial_diff") if isinstance(worker.get("artifacts"), dict) else "") or "").strip()
    stdout_excerpt = str(worker.get("stdout") or "").strip()
    if len(stdout_excerpt) > 2000:
        stdout_excerpt = stdout_excerpt[:2000] + "\n..."
    return "\n\n".join(
        part
        for part in (
            f"CRITICAL: Worker attempt {attempt + 1} failed with retryable blocker `{kind}`.",
            str(blocker.get("message") or "").strip(),
            f"Detail: {blocker.get('detail')}" if blocker.get("detail") else "",
            "Changed files from the failed attempt:\n" + changed_text,
            f"Preserved partial patch: `{patch_path}`" if patch_path else "",
            "Worker output excerpt:\n" + stdout_excerpt if stdout_excerpt else "",
            (
                "Plan a smaller repair slice that preserves any useful existing diff, then explicitly addresses the unmet "
                "acceptance criteria. Do not repeat only the same partial change, and do not mark the task complete "
                "until required tests or deterministic verification pass."
            ),
        )
        if part
    )


def _worker_incomplete_diff_extra_retries(cfg: ResolvedConfig) -> int:
    raw = str(getattr(cfg, "env", {}).get("ACA_WORKER_INCOMPLETE_DIFF_EXTRA_RETRIES", "") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("Ignoring invalid ACA_WORKER_INCOMPLETE_DIFF_EXTRA_RETRIES=%r", raw)
    return 2


def _worker_failure_can_retry(
    cfg: ResolvedConfig,
    blocker: dict[str, Any],
    attempt: int,
    base_max_loops: int,
) -> bool:
    if attempt < base_max_loops - 1:
        return True
    if str(blocker.get("kind") or "").strip() not in {
        "worker_incomplete_diff",
        "worker_corrupt_diff",
        "worker_no_progress",
    }:
        return False
    extra_retries = _worker_incomplete_diff_extra_retries(cfg)
    return attempt < base_max_loops + extra_retries - 1


def _repair_state_has_worker_incomplete_diff(ctx: "_PhaseRunContext") -> bool:
    for source in (getattr(ctx, "status", None), getattr(ctx, "blackboard", None)):
        if not isinstance(source, dict):
            continue
        repair = source.get("repair")
        if not isinstance(repair, dict):
            continue
        if str(repair.get("extra_retry_source") or "").strip() == "worker_incomplete_diff":
            return True
        sources = repair.get("extra_retry_sources")
        if isinstance(sources, list) and "worker_incomplete_diff" in sources:
            return True
        if repair.get("partial_diff_artifacts") and repair.get("partial_diff_state") == "preserved_not_accepted":
            return True
    return False


def _verification_can_retry(
    cfg: ResolvedConfig,
    ctx: "_PhaseRunContext",
    attempt: int,
    base_max_loops: int,
) -> bool:
    if attempt < base_max_loops - 1:
        return True
    if not _repair_state_has_worker_incomplete_diff(ctx):
        return False
    extra_retries = _worker_incomplete_diff_extra_retries(cfg)
    return attempt < base_max_loops + extra_retries - 1


def _partial_diff_artifacts_for_retry(worker_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for result in worker_results:
        patch_path = str(result.get("partial_diff_artifact") or "").strip()
        if not patch_path and isinstance(result.get("artifacts"), dict):
            patch_path = str(result["artifacts"].get("partial_diff") or "").strip()
        if not patch_path:
            continue
        changed_files = _collect_worker_changed_files([result])
        output_excerpt = str(result.get("stdout") or result.get("output_excerpt") or "").strip()
        if len(output_excerpt) > 2000:
            output_excerpt = output_excerpt[:2000].rstrip() + "\n..."
        subtask_id = str(result.get("subtask_id") or "").strip()
        worker_id = str(result.get("worker_id") or "").strip()
        entry: dict[str, Any] = {
            "subtask_id": subtask_id,
            "worker_id": worker_id,
            "patch_path": patch_path,
        }
        if changed_files:
            entry["changed_files"] = changed_files
        if output_excerpt:
            entry["worker_output_excerpt"] = output_excerpt
        if entry not in artifacts:
            artifacts.append(entry)
    return artifacts


def _completed_subtask_ids_for_retry(worker_results: list[dict[str, Any]]) -> list[str]:
    completed: list[str] = []
    for result in worker_results:
        status = _normalized_text(result.get("status"))
        if status not in {"completed", "skipped_existing", "tolerated_failure"} and not result.get("verified_existing"):
            continue
        subtask_id = str(result.get("subtask_id") or "").strip()
        if subtask_id and subtask_id not in completed:
            completed.append(subtask_id)
    return completed


def _prepare_pr_candidate_context(ctx: "_PhaseRunContext") -> dict[str, Any] | None:
    if not _task_mentions_external_pr_candidates(ctx.task):
        return None
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board

    contexts = _annotate_pr_candidate_current_layout(fetch_pr_contexts(ctx.cfg, ctx.task), ctx.repo_path)
    # Fetch each candidate PR head into a local ref so workers have real git
    # objects to inspect and apply (cherry-pick / checkout), not just patch text.
    pr_numbers = [
        int(context["number"])
        for context in contexts
        if not context.get("error") and str(context.get("number") or "").strip().isdigit()
    ]
    pr_refs: list[dict[str, Any]] = []
    if pr_numbers:
        try:
            pr_refs = fetch_pr_refs(ctx.cfg, ctx.repo_path, pr_numbers)
        except Exception as exc:  # ref fetch is best-effort; never block on it here
            logger.warning("Failed to fetch PR candidate refs for run %s: %s", ctx.run_id, exc)
            pr_refs = []
    artifact = {
        "task_id": ctx.task.get("task_id"),
        "title": ctx.task.get("title"),
        "source": ctx.task.get("source") or {},
        "repo": ctx.task.get("repo") or ctx.repo,
        "pull_requests": contexts,
        "fetched_refs": pr_refs,
    }
    ctx.layout["artifacts"].mkdir(parents=True, exist_ok=True)
    artifact_path = ctx.layout["artifacts"] / "pr_candidate_context.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    ctx.status.setdefault("artifacts", {})["pr_candidate_context"] = str(artifact_path)
    ctx.blackboard["pr_candidate_context"] = {
        "artifact": str(artifact_path),
        "pull_request_count": len(contexts),
        "pull_requests": contexts,
        "fetched_refs": pr_refs,
    }
    candidate_target_files = _pr_candidate_target_files(contexts)
    for subtask in ctx.planned_subtasks or []:
        subtask["pr_candidate_context_artifact"] = str(artifact_path)
        subtask["pr_candidate_context"] = contexts
        subtask["pr_candidate_refs"] = pr_refs
        if candidate_target_files:
            subtask["files"] = list(candidate_target_files)
            subtask["target_files"] = list(candidate_target_files)
            subtask["write_required"] = True
            subtask["goal"] = _pr_candidate_edit_goal(subtask.get("goal"))
    for subtask in ctx.pending_subtasks or []:
        subtask["pr_candidate_context_artifact"] = str(artifact_path)
        subtask["pr_candidate_context"] = contexts
        subtask["pr_candidate_refs"] = pr_refs
        if candidate_target_files:
            subtask["files"] = list(candidate_target_files)
            subtask["target_files"] = list(candidate_target_files)
            subtask["write_required"] = True
            subtask["goal"] = _pr_candidate_edit_goal(subtask.get("goal"))
    errors = [context for context in contexts if context.get("error")]
    if contexts and not errors:
        save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
        write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
        write_status(ctx.layout["status"], ctx.status)
        append_event(
            ctx.layout["events"],
            "github.pr_candidate_context.ready",
            ctx.run_id,
            {"artifact": str(artifact_path), "pr_count": len(contexts)},
            task_id=ctx.task.get("task_id"),
            role="manager",
            repo={"path": ctx.repo.get("path")},
        )
        return None

    detail = (
        "No referenced GitHub pull requests could be fetched."
        if not contexts
        else "; ".join(
            f"#{context.get('number')}: {context.get('error')}"
            for context in errors
            if context.get("error")
        )
    )
    msg = "GitHub PR candidate context is unavailable; ACA will not start workers without it."
    recovery = "Reconnect/refresh the GitHub MCP, verify repository access, then reset the task to Backlog."
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="planning",
        phase_detail=detail,
        run_status="blocked",
        blocker=(True, "github_context_unavailable", msg, "manager"),
        run_completed=True,
    )
    ctx.status.setdefault("blocker", {})["detail"] = detail
    ctx.status.setdefault("blocker", {})["recovery_action"] = recovery
    ctx.blackboard.setdefault("blockers", []).append(
        {
            "kind": "github_context_unavailable",
            "message": msg,
            "detail": detail,
            "recovery_action": recovery,
            "phase": "planning",
        }
    )
    _touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="blocked",
        phase="planning",
        error=msg,
        completed=True,
    )
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "github context unavailable")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(
        ctx.layout["summary"],
        build_blocked_summary(
            task_title=ctx.task["title"],
            message=f"{msg}\n\nDetail: {detail}\nRecovery: {recovery}",
        ),
    )
    _finalize_github_sync(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        outcome="blocked",
        summary=msg,
        coordination=ctx.coordination,
    )
    append_event(
        ctx.layout["events"],
        "run.blocked",
        ctx.run_id,
        {"kind": "github_context_unavailable", "detail": detail, "recovery_action": recovery},
    )
    return ctx.make_result()


def _annotate_pr_candidate_current_layout(
    contexts: list[dict[str, Any]],
    repo_path: Path,
) -> list[dict[str, Any]]:
    for context in contexts:
        if not isinstance(context, dict) or context.get("error"):
            continue
        current_files: list[str] = []
        stale_files: list[str] = []
        raw_files = context.get("files") or []
        if not isinstance(raw_files, list):
            continue
        for file_entry in raw_files:
            if not isinstance(file_entry, dict):
                continue
            path = str(file_entry.get("filename") or "").strip().lstrip("/")
            if not path:
                continue
            status = _normalized_text(file_entry.get("status"))
            base_path_exists = (repo_path / path).is_file()
            file_entry["base_path_exists"] = base_path_exists
            is_new_file = status in {"added", "created"}
            if not base_path_exists and not is_new_file:
                file_entry["current_layout_stale"] = True
                stale_files.append(path)
                continue
            current_files.append(path)
        context["current_layout_files"] = current_files
        if stale_files:
            context["stale_files"] = stale_files
    return contexts


def _pr_candidate_target_files(contexts: list[dict[str, Any]]) -> list[str]:
    excluded = {".jules/bolt.md", "jules/bolt.md"}
    files: list[str] = []
    seen: set[str] = set()
    for context in contexts:
        if not isinstance(context, dict) or context.get("error"):
            continue
        file_entries = context.get("files") if isinstance(context.get("files"), list) else []
        if file_entries:
            raw_files = [
                file_entry.get("filename")
                for file_entry in file_entries
                if isinstance(file_entry, dict) and not file_entry.get("current_layout_stale")
            ]
        else:
            raw_files = context.get("changed_files")
            if not isinstance(raw_files, list):
                raw_files = []
        for raw_file in raw_files:
            path = str(raw_file or "").strip().lstrip("/")
            if not path or path in excluded or path.startswith(".jules/"):
                continue
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _pr_candidate_contexts_from_subtasks(subtasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for subtask in subtasks or []:
        contexts = subtask.get("pr_candidate_context")
        if isinstance(contexts, list):
            return [context for context in contexts if isinstance(context, dict)]
    return []


def _pr_candidate_unexpected_changed_files(
    subtasks: list[dict[str, Any]],
    changed_files: list[str],
) -> list[str]:
    contexts = _pr_candidate_contexts_from_subtasks(subtasks)
    allowed = set(_pr_candidate_target_files(contexts))
    if not allowed:
        return []
    unexpected: list[str] = []
    for path in changed_files:
        if path and path not in allowed and path not in unexpected:
            unexpected.append(path)
    return unexpected


def _pr_candidate_edit_goal(existing_goal: Any) -> str:
    goal = str(existing_goal or "").strip()
    required = (
        "Apply the still-relevant code changes from the fetched PR candidate refs into this worktree. "
        "An applicability matrix alone is not sufficient; leave a repository diff or a structured no-safe-changes blocker."
    )
    if not goal:
        return required
    if "applicability matrix" in goal.lower() and "apply" not in goal.lower():
        return required
    if "An applicability matrix alone is not sufficient" in goal:
        return goal
    return f"{goal}\n\n{required}"


def _record_coding_run_contract(blackboard: dict[str, Any], contract: Any) -> None:
    blackboard["coding_run_contract"] = contract.as_dict()
    if getattr(contract, "code_editing", False):
        note = "Coding run contract: diff review and minimal verification are required before handoff."
        notes = blackboard.setdefault("notes", [])
        if note not in notes:
            notes.append(note)


def _record_review_policy(blackboard: dict[str, Any], cfg: ResolvedConfig) -> None:
    decision = evaluate_review_policy(cfg)
    blackboard["review_policy"] = decision.as_dict()
    note = "Review policy: human review gate required before merge."
    if decision.blocker:
        note = f"Review policy: {decision.blocker}"
    notes = blackboard.setdefault("notes", [])
    if note not in notes:
        notes.append(note)


def _coordination_store(cfg: ResolvedConfig) -> CoordinationStore:
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    return store


def _dispatch_outbox_now(
    cfg: ResolvedConfig,
    coordination: CoordinationStore,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    return dispatch_outbox_tick(cfg, coordination=coordination, limit=limit)


def _task_claim_identity(cfg: ResolvedConfig, task: dict[str, Any]) -> dict[str, str]:
    source = task.get("source") or {}
    role = "coordinator"
    worker_id = default_worker_id(cfg)
    host_id = default_host_id(cfg)
    if str(cfg.env.get("ACA_COORDINATION_ROLE") or "").strip():
        role = str(cfg.env.get("ACA_COORDINATION_ROLE") or "").strip()
    return {"worker_id": worker_id, "host_id": host_id, "role": role, "source_type": str(source.get("type") or cfg.task_source.type or "")}


def _run_start_preflight(
    cfg: ResolvedConfig,
    *,
    run_id: str,
    layout: dict[str, Path],
    repo: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    repo_path = Path(repo.get("path") or ".")
    engine_repo_path = engine_visible_path(repo_path)
    engine_run_dir = engine_visible_path(layout["run_dir"])
    result = run_command(_git_repo_args(repo_path, "status", "--short", "--branch"), env=cfg.env)
    detail = result.stderr.strip() or result.stdout.strip()
    preflight = {
        "run_id": run_id,
        "repo_path": str(repo_path),
        "repo_exists": repo_path.exists(),
        "git_metadata_exists": (repo_path / ".git").exists(),
        "engine_visible_repo_path": str(engine_repo_path),
        "engine_visible_run_dir": str(engine_run_dir),
        "logs_dir": str(layout["logs"]),
        "artifacts_dir": str(layout["artifacts"]),
        "git_status": {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    }
    layout["artifacts"].mkdir(parents=True, exist_ok=True)
    artifact_path = layout["artifacts"] / "run_start_preflight.json"
    artifact_path.write_text(json.dumps(preflight, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    preflight["artifact"] = str(artifact_path)
    append_event(
        layout["events"],
        "run.preflight",
        run_id,
        {
            "artifact": str(artifact_path),
            "repo_path": str(repo_path),
            "engine_visible_repo_path": str(engine_repo_path),
            "git_status_ok": result.returncode == 0,
        },
    )
    if result.returncode == 0:
        return preflight, None
    kind = "repo_safe_directory" if "dubious ownership" in detail.lower() else "engine_workspace_unreachable"
    return preflight, {
        "kind": kind,
        "message": detail or "Run-start repository preflight failed.",
        "recovery_action": (
            "Ensure all ACA-managed git commands use the safe.directory helper and retry."
            if kind == "repo_safe_directory"
            else "Verify the resolved repo path and ACA_ENGINE_HOST_ROOT mapping, then retry."
        ),
    }


COORDINATION_LOST_THRESHOLD = 3


def _touch_coordination(
    coordination: CoordinationStore,
    *,
    run_id: str,
    lease_id: str | None,
    lease_ttl_seconds: int,
    status: str | None = None,
    phase: str | None = None,
    error: str | None = None,
    completed: bool = False,
    ctx: "_PhaseRunContext | None" = None,
) -> bool:
    """Heartbeat the lease and update the run row.

    Returns True if the heartbeat succeeded (lease still active) or was not
    attempted (lease_id is None). Returns False if the heartbeat missed —
    callers passing ``ctx`` will see ``ctx.consecutive_heartbeat_misses``
    incremented; on the COORDINATION_LOST_THRESHOLD-th consecutive miss
    ``ctx.coordination_lost`` is flipped True so the next phase boundary can
    block the run with a clear blocker.
    """
    heartbeat_ok = True
    if lease_id:
        result = coordination.heartbeat_lease(lease_id, lease_ttl_seconds=lease_ttl_seconds)
        if result is None:
            heartbeat_ok = False
            if ctx is not None:
                ctx.consecutive_heartbeat_misses = int(ctx.consecutive_heartbeat_misses or 0) + 1
                if (
                    ctx.consecutive_heartbeat_misses >= COORDINATION_LOST_THRESHOLD
                    and not ctx.coordination_lost
                ):
                    ctx.coordination_lost = True
                    try:
                        from src.tandem_agents.runtime.runstate import append_event

                        append_event(
                            ctx.layout["events"],
                            "coordination_lost",
                            ctx.run_id,
                            {
                                "lease_id": lease_id,
                                "consecutive_misses": ctx.consecutive_heartbeat_misses,
                            },
                        )
                    except Exception:
                        # Don't let event logging failure mask the real problem.
                        pass
        elif ctx is not None:
            ctx.consecutive_heartbeat_misses = 0
    coordination.update_run(
        run_id,
        status=status,
        phase=phase,
        error=error,
        completed=completed,
    )
    return heartbeat_ok


@contextmanager
def _coordination_heartbeat(
    ctx: "_PhaseRunContext",
    *,
    phase: str,
    status: str = "running",
):
    """Keep the run lease alive while a blocking engine call is in flight."""
    stop_event = threading.Event()
    sleep_s = max(1.0, float(ctx.cfg.coordination.heartbeat_interval_seconds or 1) / 2.0)

    def _loop() -> None:
        while not stop_event.wait(sleep_s):
            _touch_coordination(
                ctx.coordination,
                run_id=ctx.run_id,
                lease_id=ctx.lease_id,
                lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                status=status,
                phase=phase,
                ctx=ctx,
            )

    _touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status=status,
        phase=phase,
        ctx=ctx,
    )
    thread = threading.Thread(target=_loop, name=f"aca-heartbeat-{phase}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


def _coordination_task_context(status: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    coordination = dict(status.get("coordination") or {})
    return (
        coordination.get("task_key"),
        coordination.get("lease_id"),
        coordination.get("worker_id"),
        coordination.get("host_id"),
        coordination.get("lease_expires_at_ms"),
    )


def _move_task_card_if_present(
    board: dict[str, Any],
    task: dict[str, Any],
    lane: str,
    actor: str,
    note: str,
) -> None:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return
    try:
        move_card(board, task_id, lane, actor, note)
    except Exception:
        logger.warning(f"Failed to move card {task_id} to {lane}", exc_info=True)
        return


def _subtask_scope_text(task: dict[str, Any], subtask: dict[str, Any]) -> str:
    parts = [
        task.get("title"),
        task.get("description"),
        subtask.get("title"),
        subtask.get("goal"),
        subtask.get("description"),
    ]
    parts.extend(_as_list(task.get("acceptance_criteria")))
    parts.extend(_as_list(subtask.get("acceptance_criteria")))
    return " ".join(str(part or "") for part in parts).lower()


def _subtask_file_score(rel_path: str, scope_text: str, original_index: int) -> tuple[int, int, str]:
    lower_rel = rel_path.lower()
    score = 0
    for token in re.findall(r"[a-z0-9]+", scope_text):
        if len(token) >= 3 and token not in {"and", "for", "the", "with"} and token in lower_rel:
            score += 4

    verification_task = any(
        token in scope_text
        for token in (
            "bug monitor",
            "quality gate",
            "quality-gate",
            "end-to-end",
            "smoke",
            "verification",
        )
    )
    test_task = any(token in scope_text for token in ("test", "tests", "regression", "coverage", "verify"))
    if test_task and "/tests/" in f"/{lower_rel}":
        score += 28
    if verification_task and lower_rel.startswith("crates/tandem-server/src/http/tests/bug_monitor"):
        score += 16
    if verification_task and "bug_monitor_parts/part03.rs" in lower_rel:
        score += 18
    if verification_task and "bug_monitor_parts/part04.rs" in lower_rel:
        score += 18
    if verification_task and lower_rel.endswith("crates/tandem-server/src/http/tests/bug_monitor.rs"):
        score -= 20
    if verification_task and lower_rel.endswith("crates/tandem-server/src/bug_monitor/service.rs"):
        score += 46
    if verification_task and lower_rel.endswith("crates/tandem-server/src/bug_monitor/types.rs"):
        score += 10
    if verification_task and re.search(r"bug_monitor_parts/part0[12]\.rs$", lower_rel):
        score -= 8
    if lower_rel.startswith("crates/tandem-server/src/bug_monitor/"):
        score += 10
    if lower_rel.startswith("scripts/bug-monitor"):
        score += 8 if "smoke" in scope_text else -4
    if lower_rel.startswith("docs/internal/"):
        score -= 20
    return (-score, original_index, lower_rel)


def _compact_overbroad_single_worker_subtask(
    task: dict[str, Any],
    subtask: dict[str, Any],
    discovered_files: list[str] | None,
    max_files: int = 3,
) -> dict[str, Any]:
    files = [str(entry).strip() for entry in _as_list(subtask.get("files")) if str(entry).strip()]
    target_files = [str(entry).strip() for entry in _as_list(subtask.get("target_files")) if str(entry).strip()]
    declared = list(dict.fromkeys(files + target_files))
    if len(declared) <= max_files:
        return subtask

    candidates = list(dict.fromkeys(declared + list(discovered_files or [])))
    scope_text = _subtask_scope_text(task, subtask)
    ranked = sorted(
        enumerate(candidates),
        key=lambda item: _subtask_file_score(item[1], scope_text, item[0]),
    )
    selected = [path for _, path in ranked[:max_files]]
    if not selected or selected == declared:
        return subtask

    compacted = dict(subtask)
    compacted["files"] = selected
    compacted["target_files"] = selected
    compacted["scope_note"] = (
        "ACA narrowed an overbroad one-worker target set from "
        f"{len(declared)} files to {len(selected)} high-signal files. Stay inside this slice unless a directly adjacent "
        "tracked source or test file is required for the same acceptance criterion."
    )
    compacted["discovered_context_files"] = [path for path in candidates if path not in selected][:12]
    return compacted


def _merge_manager_subtasks_for_single_worker(
    task: dict[str, Any],
    subtasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse a manager plan into one coherent serial worker contract.

    Disabled swarm mode runs workers serially, but each worker still uses an
    isolated worktree. Multiple manager subtasks that create or edit a shared
    crate/module tree can overwrite each other or miss prerequisite files when
    they are dispatched as separate worktrees. In single-worker mode, preserve
    all manager coverage by merging the contracts into one worker instead of
    dropping or independently syncing slices.
    """
    if len(subtasks) <= 1:
        return subtasks

    def _extend_unique(target: list[str], values: Any) -> None:
        for value in _as_list(values):
            text = str(value or "").strip()
            if text and text not in target:
                target.append(text)

    files: list[str] = []
    target_files: list[str] = []
    ignored_target_files: list[str] = []
    acceptance_criteria: list[str] = []
    deliverables: list[str] = []
    verification_commands: list[str] = []
    dependencies: list[str] = []
    in_scope: list[str] = []
    out_of_scope: list[str] = []
    scope_notes: list[str] = []
    titles: list[str] = []
    goals: list[str] = []

    for subtask in subtasks:
        _extend_unique(files, subtask.get("files"))
        _extend_unique(target_files, subtask.get("target_files"))
        _extend_unique(ignored_target_files, subtask.get("ignored_target_files"))
        _extend_unique(acceptance_criteria, subtask.get("acceptance_criteria"))
        _extend_unique(deliverables, subtask.get("deliverables"))
        _extend_unique(verification_commands, subtask.get("verification_commands"))
        _extend_unique(dependencies, subtask.get("dependencies"))
        _extend_unique(in_scope, subtask.get("in_scope"))
        _extend_unique(out_of_scope, subtask.get("out_of_scope"))
        title = str(subtask.get("title") or "").strip()
        if title:
            titles.append(title)
        goal = str(subtask.get("goal") or subtask.get("local_goal") or "").strip()
        if goal:
            goals.append(goal)
        scope_note = str(subtask.get("scope_note") or "").strip()
        if scope_note:
            scope_notes.append(scope_note)

    if not target_files and files:
        target_files = list(files)

    combined_goal = str(task.get("local_goal") or task.get("title") or "").strip()
    if goals:
        combined_goal = (
            f"{combined_goal}\n\nMerged manager subtasks:\n"
            + "\n".join(f"- {goal}" for goal in goals)
        ).strip()

    scope_note = (
        "ACA merged multiple manager subtasks into one serial worker because swarm mode is disabled. "
        "Implement the combined contract in one coherent worktree so shared files, new crate/module boundaries, "
        "tests, and documentation stay consistent."
    )
    if scope_notes:
        scope_note += "\n" + "\n".join(scope_notes)

    merged = {
        "id": "subtask-1",
        "title": str(task.get("title") or "Combined manager implementation").strip()
        or "Combined manager implementation",
        "goal": combined_goal or "Complete the combined manager implementation.",
        "description": "\n".join(titles),
        "acceptance_criteria": acceptance_criteria,
        "deliverables": deliverables,
        "files": files,
        "target_files": target_files,
        "ignored_target_files": ignored_target_files,
        "verification_commands": verification_commands,
        "dependencies": dependencies,
        "program_goal": str(task.get("program_goal") or "").strip() or None,
        "local_goal": str(task.get("local_goal") or task.get("title") or "").strip(),
        "in_scope": in_scope,
        "out_of_scope": out_of_scope,
        "status": "pending",
        "scope_note": scope_note,
        "merged_subtasks": subtasks,
    }
    return [merged]


def _normalize_manager_subtasks(
    task: dict[str, Any],
    raw_subtasks: list[dict[str, Any]],
    repo_path: str,
    discovered_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    repo_prefix = str(Path(repo_path)).rstrip("/") + "/"
    contract = task_contract_payload(task)
    declared_task_target_files = [
        str(entry).strip()
        for entry in _as_list(contract.get("target_files") or task.get("target_files"))
        if str(entry).strip()
    ]
    task_target_files = list(declared_task_target_files)
    if not task_target_files:
        task_target_files = _explicit_task_target_files(Path(repo_path), task)
    contract_constrained = bool(declared_task_target_files)
    suppress_discovered_targets = _task_mentions_external_pr_candidates(task) and not contract_constrained

    def _normalize_repo_file(entry: str) -> str:
        if entry.startswith(repo_prefix):
            return entry[len(repo_prefix) :]
        if entry.startswith("/"):
            return Path(entry).name
        return entry

    def _git_ignored_repo_file(entry: str) -> bool:
        rel_path = str(entry or "").strip()
        if not rel_path or rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../") or "/../" in f"/{rel_path}/":
            return False
        result = run_command(_git_repo_args(Path(repo_path), "check-ignore", "--quiet", "--", rel_path))
        return result.returncode == 0

    def _filter_ignored_files(entries: list[str]) -> tuple[list[str], list[str]]:
        kept: list[str] = []
        ignored: list[str] = []
        for entry in entries:
            if _git_ignored_repo_file(entry):
                if entry not in ignored:
                    ignored.append(entry)
                continue
            if entry not in kept:
                kept.append(entry)
        return kept, ignored

    def _repo_file_exists(entry: str) -> bool:
        rel_path = str(entry or "").strip()
        return bool(rel_path) and (Path(repo_path) / rel_path).is_file()

    def _missing_target_can_be_created(entry: str) -> bool:
        rel_path = str(entry or "").strip().replace("\\", "/").lower()
        if not rel_path:
            return False
        if rel_path.startswith(".github/workflows/") and rel_path.endswith((".yml", ".yaml")):
            return True
        if "/tests/" in f"/{rel_path}/" or rel_path.startswith("tests/"):
            return True
        name = Path(rel_path).name
        return name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))

    def _drop_hallucinated_missing_source_targets(entries: list[str]) -> tuple[list[str], list[str]]:
        if contract_constrained or not any(_repo_file_exists(entry) for entry in entries):
            return entries, []
        kept: list[str] = []
        dropped: list[str] = []
        for entry in entries:
            if _repo_file_exists(entry) or _missing_target_can_be_created(entry):
                kept.append(entry)
            elif entry not in dropped:
                dropped.append(entry)
        return kept, dropped

    normalized_task_target_files = [_normalize_repo_file(entry) for entry in task_target_files]
    normalized_task_target_files, ignored_task_target_files = _filter_ignored_files(normalized_task_target_files)
    for index, item in enumerate(raw_subtasks, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or f"Subtask {index}").strip()
        description = str(item.get("description") or "").strip()
        goal = str(item.get("goal") or description or title or task.get("title") or f"Subtask {index}").strip()
        deliverables = [
            str(entry).strip()
            for entry in _as_list(item.get("deliverables") or item.get("deliverable") or task.get("deliverables"))
            if str(entry).strip()
        ]
        acceptance = item.get("acceptance_criteria")
        if not acceptance:
            acceptance = item.get("acceptance")
        if not acceptance:
            acceptance = item.get("acceptance_checklist")
        if not acceptance:
            acceptance = item.get("validation")
        if not acceptance:
            acceptance = item.get("required_work")
        if not acceptance:
            acceptance = item.get("scope")
        if not acceptance:
            acceptance = item.get("objective")
        if not acceptance:
            acceptance = item.get("verification")
        if not acceptance:
            acceptance = item.get("expected_verification")
        if not acceptance:
            acceptance = item.get("instructions")
        if not acceptance:
            acceptance = item.get("handoff")
        if not acceptance:
            acceptance = deliverables
        acceptance_criteria = [str(entry).strip() for entry in _as_list(acceptance) if str(entry).strip()]
        raw_files = [str(entry).strip() for entry in _as_list(item.get("files")) if str(entry).strip()]
        raw_target_files = [str(entry).strip() for entry in _as_list(item.get("target_files")) if str(entry).strip()]
        verification_commands = [
            str(entry).strip()
            for entry in _as_list(item.get("verification_commands") or item.get("verification") or task.get("verification_commands"))
            if str(entry).strip()
        ]
        dependencies = [
            str(entry).strip()
            for entry in _as_list(item.get("dependencies") or task.get("dependencies"))
            if str(entry).strip()
        ]
        in_scope = [
            str(entry).strip()
            for entry in _as_list(item.get("in_scope") or task.get("in_scope"))
            if str(entry).strip()
        ]
        out_of_scope = [
            str(entry).strip()
            for entry in _as_list(item.get("out_of_scope") or task.get("out_of_scope"))
            if str(entry).strip()
        ]
        normalized_files: list[str] = []
        for entry in raw_files:
            normalized_files.append(_normalize_repo_file(entry))
        normalized_target_files: list[str] = []
        for entry in raw_target_files:
            normalized_target_files.append(_normalize_repo_file(entry))
        normalized_files, ignored_files = _filter_ignored_files(normalized_files)
        normalized_target_files, ignored_target_files = _filter_ignored_files(normalized_target_files)
        ignored_files = list(dict.fromkeys(ignored_files + ignored_target_files + ignored_task_target_files))
        manifest_only_after_ignored_docs = (
            bool(ignored_files)
            and set(normalized_files + normalized_target_files) == {"Cargo.toml"}
            and not any(
                str(entry or "").strip().replace("\\", "/").startswith("crates/")
                for entry in raw_files + raw_target_files
            )
        )
        scope_note = ""
        if manifest_only_after_ignored_docs:
            normalized_files = []
            normalized_target_files = []
            scope_note = (
                "ACA removed root Cargo.toml as the only remaining target after filtering git-ignored docs. "
                "Do not satisfy this task by placing a prose specification in workspace metadata; discover or create "
                "tracked implementation, test, or public documentation files instead."
            )
        if not normalized_files and normalized_target_files:
            normalized_files = list(normalized_target_files)
        normalized_files, dropped_missing_files = _drop_hallucinated_missing_source_targets(normalized_files)
        normalized_target_files, dropped_missing_target_files = _drop_hallucinated_missing_source_targets(normalized_target_files)
        dropped_missing = list(dict.fromkeys(dropped_missing_files + dropped_missing_target_files))
        if dropped_missing:
            extra = (
                "ACA dropped non-existing manager file targets because this subtask already has an existing "
                "source target and the missing paths were not recognized test/workflow files: "
                + ", ".join(dropped_missing)
                + "."
            )
            scope_note = f"{scope_note}\n{extra}".strip()
        if contract_constrained:
            allowed = set(normalized_task_target_files)
            if not normalized_files or any(entry not in allowed for entry in normalized_files):
                normalized_files = list(normalized_task_target_files)
        if contract_constrained:
            normalized_target_files = list(normalized_task_target_files)
        elif not normalized_target_files and normalized_files:
            normalized_target_files = list(normalized_files)
        normalized.append(
            {
                "id": str(item.get("id") or item.get("subtask_id") or f"subtask-{index}").strip(),
                "title": title,
                "goal": goal,
                "description": description,
                "acceptance_criteria": acceptance_criteria,
                "deliverables": deliverables,
                "files": normalized_files,
                "target_files": normalized_target_files,
                "ignored_target_files": ignored_files,
                "verification_commands": verification_commands,
                "dependencies": dependencies,
                "program_goal": str(item.get("program_goal") or task.get("program_goal") or "").strip() or None,
                "local_goal": str(item.get("local_goal") or task.get("local_goal") or goal).strip(),
                "in_scope": in_scope,
                "out_of_scope": out_of_scope,
                "status": str(item.get("status") or "pending").strip(),
                "scope_note": scope_note,
            }
        )
    if normalized:
        if discovered_files and not suppress_discovered_targets:
            chunks = [discovered_files[i::len(normalized)] for i in range(len(normalized))]
            for index, item in enumerate(normalized):
                if item.get("files"):
                    continue
                item["files"] = chunks[index] or list(discovered_files)
        return normalized
    fallback = derive_subtasks(task, 1)
    if discovered_files and fallback and not suppress_discovered_targets:
        if contract_constrained:
            fallback[0]["files"] = list(normalized_task_target_files)
            fallback[0]["target_files"] = list(normalized_task_target_files)
        else:
            fallback[0]["files"] = list(discovered_files)
    return fallback


def _prepare_subtasks_with_discovery(
    task: dict[str, Any],
    manager_plan: dict[str, Any],
    repo_path: Path,
    max_workers: int,
    *,
    merge_manager_subtasks: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    discovered_files = discover_repo_files(repo_path, task, limit=12)
    manager_subtasks = list(manager_plan.get("subtasks") or [])
    subtasks = _normalize_manager_subtasks(
        task,
        manager_subtasks,
        str(repo_path),
        discovered_files,
    )
    has_manager_subtasks = bool(subtasks)
    if not subtasks:
        subtasks = derive_subtasks(task, max_workers)
        contract = task_contract_payload(task)
        task_target_files = [
            str(entry).strip()
            for entry in _as_list(contract.get("target_files") or task.get("target_files"))
            if str(entry).strip()
        ]
        if not task_target_files:
            task_target_files = _explicit_task_target_files(repo_path, task)
        if task_target_files:
            subtasks[0]["files"] = list(task_target_files)
            subtasks[0]["target_files"] = list(task_target_files)
        elif discovered_files and not _task_mentions_external_pr_candidates(task):
            subtasks[0]["files"] = list(discovered_files)
    if max_workers <= 1 and len(subtasks) == 1:
        subtasks = [
            _compact_overbroad_single_worker_subtask(task, subtask, discovered_files)
            for subtask in subtasks
        ]
    elif max_workers <= 1 and merge_manager_subtasks and has_manager_subtasks and len(subtasks) > 1:
        subtasks = _merge_manager_subtasks_for_single_worker(task, subtasks)
    if has_manager_subtasks:
        return discovered_files, subtasks
    return discovered_files, subtasks[: max(1, max_workers)]


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", [], (), {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _collect_expected_repo_files(subtasks: list[dict[str, Any]]) -> list[str]:
    return collect_expected_repo_files(subtasks)


def _sticky_expected_repo_files(
    blackboard: dict[str, Any],
    expected_files: list[str],
) -> list[str]:
    previous: list[str] = []
    stored = blackboard.get("expected_repo_files")
    if isinstance(stored, list):
        previous.extend(str(path).strip() for path in stored if str(path).strip())
    repo_validation = blackboard.get("repo_validation")
    if isinstance(repo_validation, dict):
        previous.extend(
            str(path).strip()
            for path in (repo_validation.get("expected_files") or [])
            if str(path).strip()
        )
    merged = list(
        dict.fromkeys(
            [
                str(path).strip()
                for path in [*previous, *expected_files]
                if str(path).strip()
            ]
        )
    )
    blackboard["expected_repo_files"] = merged
    return merged


def _collect_worker_changed_files(worker_results: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for result in worker_results:
        for raw_path in result.get("changed_files") or []:
            rel_path = str(raw_path or "").strip().replace("\\", "/")
            while rel_path.startswith("./"):
                rel_path = rel_path[2:]
            if not rel_path or rel_path in seen:
                continue
            if rel_path.startswith("/") or rel_path == ".." or rel_path.startswith("../") or "/../" in f"/{rel_path}/":
                continue
            seen.add(rel_path)
            files.append(rel_path)
    return files


def _validation_expected_repo_files(
    repo_path: Path,
    expected_files: list[str],
    changed_files: list[str],
) -> list[str]:
    """Return concrete files deterministic validation should require.

    Manager plans often contain candidate target paths. After a worker has
    produced a diff, a missing untouched candidate should not force a retry into
    creating that speculative file. Validate files that already exist plus files
    the worker actually changed.
    """

    def _normalize(raw_path: str) -> str:
        rel_path = str(raw_path).strip().replace("\\", "/")
        while rel_path.startswith("./"):
            rel_path = rel_path[2:]
        return rel_path

    normalized_expected = [_normalize(str(path)) for path in expected_files if str(path).strip()]
    normalized_changed = [_normalize(str(path)) for path in changed_files if str(path).strip()]
    normalized_changed_set = set(normalized_changed)
    if not normalized_changed:
        return list(dict.fromkeys(normalized_expected))

    concrete: list[str] = []
    for rel_path in [*normalized_expected, *normalized_changed]:
        if not rel_path or rel_path in concrete:
            continue
        if rel_path in normalized_changed_set or (repo_path / rel_path).is_file():
            concrete.append(rel_path)
    return concrete


def _upsert_worker_result(collection: list[dict[str, Any]], result: dict[str, Any]) -> None:
    identity = str(result.get("subtask_id") or result.get("worker_id") or "").strip()
    if not identity:
        collection.append(result)
        return
    for index, existing in enumerate(collection):
        existing_identity = str(existing.get("subtask_id") or existing.get("worker_id") or "").strip()
        if existing_identity == identity:
            collection[index] = result
            return
    collection.append(result)


def _record_worker_result(
    blackboard: dict[str, Any],
    worker_results: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    _upsert_worker_result(worker_results, result)
    _upsert_worker_result(blackboard.setdefault("workers", []), result)


def _worker_result_metrics(worker_results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "completed_workers": 0,
        "failed_workers": 0,
        "skipped_workers": 0,
        "tolerated_workers": 0,
    }
    for result in worker_results:
        status = _normalized_text(result.get("status"))
        if status == "completed":
            counts["completed_workers"] += 1
        elif status == "failed":
            counts["failed_workers"] += 1
        elif status == "skipped_existing":
            counts["skipped_workers"] += 1
        elif status == "tolerated_failure":
            counts["tolerated_workers"] += 1
    return counts


def _deterministic_repo_validation(repo_path: Path, expected_files: list[str]) -> dict[str, Any]:
    return deterministic_repo_validation(repo_path, expected_files)


def _repo_validation_blocker_message(repo_validation: dict[str, Any]) -> str | None:
    return repo_validation_blocker_message(repo_validation)


def _has_verifiable_worker_success(worker_results: list[dict[str, Any]]) -> bool:
    for result in worker_results:
        status = _normalized_text(result.get("status"))
        if status in {"skipped_existing", "tolerated_failure"}:
            return True
        if result.get("verified_existing"):
            return True
    return False


def _has_unresolved_write_required_worker_failure(worker_results: list[dict[str, Any]]) -> bool:
    for result in worker_results:
        if _normalized_text(result.get("status")) != "failed":
            continue
        if result.get("write_required") is False:
            continue
        if result.get("verified_existing"):
            continue
        return True
    return False


def _execute_local_worker_pool(
    cfg: ResolvedConfig,
    run_id: str,
    repo_path: Path,
    run_dir: Path,
    task: dict[str, Any],
    pending_subtasks: list[dict[str, Any]],
    worker_limit: int,
    *,
    worker_runner: Callable[
        [ResolvedConfig, str, Path, Path, dict[str, Any], dict[str, Any], str, int],
        dict[str, Any],
    ] = run_worker_subtask,
    on_start: Callable[[str, dict[str, Any]], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
    worker_timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    if not pending_subtasks:
        return []
    results: list[dict[str, Any]] = []
    timeout = float(worker_timeout_seconds or 0)

    def _run_one(index: int, subtask: dict[str, Any], worker_id: str) -> dict[str, Any]:
        if on_start is not None:
            try:
                on_start(worker_id, subtask)
            except Exception:
                logger.exception(
                    "Worker start callback failed for %s/%s; continuing worker execution.",
                    worker_id,
                    subtask.get("id"),
                )
        try:
            result = worker_runner(cfg, run_id, repo_path, run_dir, task, subtask, worker_id, index)
        except Exception as exc:
            result = {
                "worker_id": worker_id,
                "subtask_index": index,
                "subtask_id": subtask["id"],
                "title": subtask["title"],
                "status": "failed",
                "returncode": 1,
                "worktree": "",
                "log_path": "",
                "output_excerpt": f"Worker execution raised an exception: {exc}",
                "write_required": bool(subtask.get("write_required", True)),
                "verified_existing": False,
            }
        if not isinstance(result, dict):
            result = {}
        result.setdefault("worker_id", worker_id)
        result.setdefault("subtask_index", index)
        result.setdefault("subtask_id", subtask["id"])
        result.setdefault("title", subtask["title"])
        result.setdefault("status", "failed" if result.get("returncode", 1) else "completed")
        result.setdefault("returncode", 0 if _normalized_text(result.get("status")) == "completed" else 1)
        subtask_write_required = bool(subtask.get("write_required", True))
        result["write_required"] = subtask_write_required or bool(result.get("write_required"))
        result.setdefault("verified_existing", False)
        return result

    def _record_result(result: dict[str, Any]) -> None:
        results.append(result)
        if on_result is not None:
            try:
                on_result(result)
            except Exception:
                logger.exception(
                    "Worker result callback failed for %s/%s; keeping result in local pool output.",
                    result.get("worker_id"),
                    result.get("subtask_id"),
                )

    def _timeout_result(index: int, subtask: dict[str, Any], worker_id: str) -> dict[str, Any]:
        return {
            "worker_id": worker_id,
            "subtask_index": index,
            "subtask_id": subtask["id"],
            "title": subtask["title"],
            "status": "failed",
            "returncode": 1,
            "worktree": "",
            "log_path": str(run_dir / "logs" / f"{worker_id}.log"),
            "output_excerpt": (
                f"Worker produced no terminal result within {timeout:.0f}s. "
                "ACA blocked the run instead of leaving it in worker_execution."
            ),
            "failure_reason": f"Worker produced no terminal result within {timeout:.0f}s.",
            "blocker_kind": "worker_no_progress",
            "recovery_action": "Inspect the worker log and engine run state, then retry after fixing the stalled worker path.",
            "write_required": bool(subtask.get("write_required", True)),
            "verified_existing": False,
        }

    if worker_limit <= 1:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            for index, subtask in enumerate(pending_subtasks, start=1):
                worker_id = f"worker-{index}"
                future = executor.submit(_run_one, index, subtask, worker_id)
                try:
                    result = future.result(timeout=timeout if timeout > 0 else None)
                except FutureTimeoutError:
                    future.cancel()
                    result = _timeout_result(index, subtask, worker_id)
                    _record_result(result)
                    break
                _record_result(result)
                if _has_unresolved_write_required_worker_failure([result]):
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return results

    executor = ThreadPoolExecutor(max_workers=max(1, worker_limit))
    futures = {
        executor.submit(_run_one, index, subtask, f"worker-{index}"): (
            index,
            subtask,
            f"worker-{index}",
        )
        for index, subtask in enumerate(pending_subtasks, start=1)
    }
    try:
        completed = set()
        try:
            completed_iter = as_completed(futures, timeout=timeout if timeout > 0 else None)
            for future in completed_iter:
                completed.add(future)
                index, subtask, worker_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "worker_id": worker_id,
                        "subtask_index": index,
                        "subtask_id": subtask["id"],
                        "title": subtask["title"],
                        "status": "failed",
                        "returncode": 1,
                        "worktree": "",
                        "log_path": "",
                        "output_excerpt": f"Worker execution raised an exception: {exc}",
                        "write_required": bool(subtask.get("write_required", True)),
                        "verified_existing": False,
                    }
                if not isinstance(result, dict):
                    result = {}
                result.setdefault("worker_id", worker_id)
                result.setdefault("subtask_index", index)
                result.setdefault("subtask_id", subtask["id"])
                result.setdefault("title", subtask["title"])
                result.setdefault("status", "failed" if result.get("returncode", 1) else "completed")
                result.setdefault("returncode", 0 if _normalized_text(result.get("status")) == "completed" else 1)
                subtask_write_required = bool(subtask.get("write_required", True))
                result["write_required"] = subtask_write_required or bool(result.get("write_required"))
                result.setdefault("verified_existing", False)
                _record_result(result)
        except FutureTimeoutError:
            for future, (index, subtask, worker_id) in futures.items():
                if future in completed:
                    continue
                future.cancel()
                _record_result(_timeout_result(index, subtask, worker_id))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _all_subtasks_verified_existing(
    subtasks: list[dict[str, Any]],
    worker_results: list[dict[str, Any]],
    repo_validation: dict[str, Any] | None = None,
    task: dict[str, Any] | None = None,
) -> bool:
    if not subtasks or not worker_results:
        return False
    source = (task or {}).get("source") if isinstance(task, dict) else {}
    source_type = str(source.get("type") or "").strip() if isinstance(source, dict) else ""
    execution_kind = str((task or {}).get("execution_kind") or "").strip() if isinstance(task, dict) else ""
    if source_type == "github_project" or (source_type == "linear" and execution_kind == "code_edit"):
        return False
    if repo_validation is not None and not repo_validation.get("ok"):
        return False
    status_by_subtask_id: dict[str, str] = {}
    for result in worker_results:
        subtask_id = str(result.get("subtask_id") or "").strip()
        if not subtask_id:
            continue
        status_by_subtask_id[subtask_id] = _normalized_text(result.get("status"))
    if len(status_by_subtask_id) < len(subtasks):
        return False
    return all(
        status_by_subtask_id.get(str(subtask.get("id") or "").strip())
        in {"skipped_existing", "tolerated_failure"}
        for subtask in subtasks
    )


def _review_blocker_message(
    review_result: dict[str, Any],
    repo_validation: dict[str, Any] | None = None,
) -> str | None:
    return review_blocker_message(review_result, repo_validation=repo_validation)


def _test_blocker_message(test_result: dict[str, Any], repo_validation: dict[str, Any] | None = None) -> str | None:
    return test_blocker_message(test_result, repo_validation=repo_validation)


def _init_github_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    scope: str,
    remote_sync: str,
) -> dict[str, Any]:
    status["github_mcp"] = {
        "scope": scope,
        "remote_sync": remote_sync,
        "connected": None,
        "last_action": "initialized",
        "warnings": [],
    }
    write_status(layout["status"], status)
    return status


def _update_github_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    connected: bool | None,
    last_action: str,
    warning: str | None = None,
) -> dict[str, Any]:
    github_state = status.setdefault(
        "github_mcp",
        {"scope": "none", "remote_sync": "off", "connected": None, "last_action": "initialized", "warnings": []},
    )
    github_state["connected"] = connected
    github_state["last_action"] = last_action
    if warning:
        github_state.setdefault("warnings", []).append(warning)
    write_status(layout["status"], status)
    return status


def _record_github_warning(
    *,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    message: str,
) -> None:
    append_event(layout["events"], "github_mcp.warning", run_id, {"message": message})
    _update_github_mcp_status(status, layout, connected=None, last_action="warning", warning=message)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP warning: {message}")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)


def _init_linear_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    scope: str,
    remote_sync: str,
) -> dict[str, Any]:
    status["linear_mcp"] = {
        "server": "",
        "scope": scope,
        "remote_sync": remote_sync,
        "connected": None,
        "last_action": "initialized",
        "warnings": [],
    }
    write_status(layout["status"], status)
    return status


def _update_linear_mcp_status(
    status: dict[str, Any],
    layout: dict[str, Path],
    *,
    connected: bool | None,
    last_action: str,
    warning: str | None = None,
    server: str = "",
) -> dict[str, Any]:
    linear_state = status.setdefault(
        "linear_mcp",
        {"server": "", "scope": "none", "remote_sync": "off", "connected": None, "last_action": "initialized", "warnings": []},
    )
    if server:
        linear_state["server"] = server
    linear_state["connected"] = connected
    linear_state["last_action"] = last_action
    if warning:
        linear_state.setdefault("warnings", []).append(warning)
    write_status(layout["status"], status)
    return status


def _record_linear_warning(
    *,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    message: str,
) -> None:
    append_event(layout["events"], "linear_mcp.warning", run_id, {"message": message})
    _update_linear_mcp_status(status, layout, connected=None, last_action="warning", warning=message)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"Linear MCP warning: {message}")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)


def _connect_linear_for_phase(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
    required: bool,
) -> bool:
    server_name = linear_mcp_server_name(cfg)
    try:
        ensure_linear_mcp_connected(cfg)
    except Exception as exc:
        if required:
            raise
        if status is not None:
            _record_linear_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message=str(exc),
            )
        return False
    append_event(layout["events"], event_type, run_id, {"connected": True, "server": server_name})
    if status is not None:
        _update_linear_mcp_status(status, layout, connected=True, last_action=event_type, server=server_name)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"Linear MCP connected for phase `{event_type}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    return True


def _disconnect_linear_for_coding(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
) -> None:
    # Linear is ACA's task source and status/comment sink. Keep the
    # operator-authenticated MCP connection durable across runs instead of
    # disconnecting it after finalize or GitHub sync cleanup.
    return


def _connect_github_for_phase(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
    required: bool,
) -> bool:
    try:
        ensure_github_mcp_connected(cfg)
    except Exception as exc:
        if required:
            raise
        if status is not None:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message=str(exc),
            )
        return False
    append_event(layout["events"], event_type, run_id, {"connected": True})
    if status is not None:
        _update_github_mcp_status(status, layout, connected=True, last_action=event_type)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP connected for phase `{event_type}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    return True


def _disconnect_github_for_coding(
    *,
    cfg: ResolvedConfig,
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any] | None,
    blackboard: dict[str, Any] | None,
    event_type: str,
) -> None:
    server = get_mcp_server(cfg, "github")
    if not server or not server.get("connected"):
        return
    try:
        ensure_github_mcp_disconnected(cfg)
    except Exception as exc:
        if status is not None:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message=str(exc),
            )
        return
    append_event(layout["events"], event_type, run_id, {"connected": False})
    if status is not None:
        _update_github_mcp_status(status, layout, connected=False, last_action=event_type)
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"GitHub MCP disconnected for phase `{event_type}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)


def _sync_github_claim_status(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any],
    remote_sync: str,
    coordination: CoordinationStore | None = None,
) -> None:
    if remote_sync == "off":
        return
    if coordination is not None:
        coordination.enqueue_outbox(
            kind="github_project.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "target_status": github_project_status_name_for_task_state("active"),
                "task": task,
            },
            dedupe_key=f"{run_id}:github:claim",
        )
        summary = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            if str(result.get("kind") or "") != "github_project.status_update":
                continue
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            append_event(layout["events"], "github_project.status_updated", run_id, {"status": payload.get("target_status") or github_project_status_name_for_task_state("active")})
            source = task.get("source") or {}
            owner = str(source.get("owner") or "").strip()
            project = source.get("project")
            if owner and project not in (None, ""):
                try:
                    invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
                except Exception:
                    logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
            _append_blackboard_note(blackboard, f"GitHub Project status updated to `{payload.get('target_status') or github_project_status_name_for_task_state('active')}`.")
        if terminal_failure:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="GitHub Project claim status could not be dispatched from the outbox.",
            )
        return
    target_status = github_project_status_name_for_task_state("active")
    warning = update_project_item_status(cfg, task, target_status)
    if warning:
        _record_github_warning(run_id=run_id, layout=layout, status=status, blackboard=blackboard, message=warning)
        return
    append_event(layout["events"], "github_project.status_updated", run_id, {"status": target_status})
    source = task.get("source") or {}
    owner = str(source.get("owner") or "").strip()
    project = source.get("project")
    if owner and project not in (None, ""):
        try:
            invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
        except Exception:
            logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
    _append_blackboard_note(blackboard, f"GitHub Project status updated to `{target_status}`.")
    save_blackboard(layout["blackboard"], blackboard)
    write_blackboard_snapshot(layout["run_dir"], blackboard)


def _linear_update_fields_for_status(cfg: ResolvedConfig, target_status: str, labels: list[str] | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "status": target_status,
        "state": target_status,
        "state_name": target_status,
    }
    clean_labels = [str(label).strip() for label in (labels or []) if str(label).strip()]
    if clean_labels:
        fields["labels"] = clean_labels
        fields["label_names"] = clean_labels
    return fields


def _sync_linear_claim_status(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any],
    remote_sync: str,
    coordination: CoordinationStore | None = None,
) -> None:
    if remote_sync == "off":
        return
    target_status = linear_status_name_for_task_state(cfg, "active")
    labels = [cfg.linear_mcp.claim_label] if str(cfg.linear_mcp.claim_label or "").strip() else []
    if coordination is not None:
        coordination.enqueue_outbox(
            kind="linear_issue.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "target_status": target_status,
                "labels": labels,
                "task": task,
            },
            dedupe_key=f"{run_id}:linear:claim",
        )
        summary = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            if str(result.get("kind") or "") != "linear_issue.status_update":
                continue
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            append_event(layout["events"], "linear_issue.status_updated", run_id, {"status": payload.get("target_status") or target_status})
            _append_blackboard_note(blackboard, f"Linear issue status updated to `{payload.get('target_status') or target_status}`.")
        if terminal_failure:
            _record_linear_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="Linear claim status could not be dispatched from the outbox.",
            )
        return
    warning = linear_update_issue(cfg, task, _linear_update_fields_for_status(cfg, target_status, labels))
    if warning:
        _record_linear_warning(run_id=run_id, layout=layout, status=status, blackboard=blackboard, message=warning)
        return
    append_event(layout["events"], "linear_issue.status_updated", run_id, {"status": target_status})
    _append_blackboard_note(blackboard, f"Linear issue status updated to `{target_status}`.")
    save_blackboard(layout["blackboard"], blackboard)
    write_blackboard_snapshot(layout["run_dir"], blackboard)


def _finalize_linear_sync(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    outcome: str,
    summary: str,
    diff_snapshot: str | None = None,
    review_returncode: int | None = None,
    test_returncode: int | None = None,
    coordination: CoordinationStore | None = None,
) -> bool:
    source_type = str((task.get("source") or {}).get("type") or cfg.task_source.type)
    remote_sync = linear_remote_sync_mode(cfg, source_type)
    scope = linear_mcp_scope(cfg, source_type)
    if remote_sync == "off" or scope not in {"intake_finalize", "always"}:
        return False
    target_status = linear_status_name_for_outcome(cfg, outcome)
    labels: list[str] = []
    if outcome == "completed" and str(cfg.linear_mcp.done_label or "").strip():
        labels.append(cfg.linear_mcp.done_label)
    elif outcome != "completed" and str(cfg.linear_mcp.blocked_label or "").strip():
        labels.append(cfg.linear_mcp.blocked_label)
    if coordination is not None:
        coordination.enqueue_outbox(
            kind="linear_issue.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": outcome,
                "summary": summary,
                "target_status": target_status,
                "labels": labels,
                "task": task,
            },
            dedupe_key=f"{run_id}:linear:finalize-status",
        )
        if remote_sync in {"status_comment", "rich"}:
            comment_body = build_linear_comment_body(
                run_id=run_id,
                task_title=task.get("title") or "Linear task",
                outcome=outcome,
                summary=summary,
                diff_snapshot=diff_snapshot,
                review_returncode=review_returncode,
                test_returncode=test_returncode,
            )
            coordination.enqueue_outbox(
                kind="linear_issue.comment",
                aggregate_type="task",
                aggregate_id=str(task.get("task_id") or run_id),
                payload={
                    "run_id": run_id,
                    "outcome": outcome,
                    "summary": summary,
                    "diff_snapshot": diff_snapshot,
                    "review_returncode": review_returncode,
                    "test_returncode": test_returncode,
                    "body": comment_body,
                    "task": task,
                },
                dedupe_key=f"{run_id}:linear:finalize-comment",
            )
        summary_result = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary_result.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            kind = str(result.get("kind") or "").strip()
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            if kind == "linear_issue.status_update":
                status_name = payload.get("target_status") or target_status
                append_event(layout["events"], "linear_issue.status_updated", run_id, {"status": status_name})
                if blackboard is not None:
                    _append_blackboard_note(blackboard, f"Linear issue status updated to `{status_name}`.")
            elif kind == "linear_issue.comment":
                append_event(layout["events"], "linear_issue.comment_added", run_id, {"outcome": outcome})
                if blackboard is not None:
                    _append_blackboard_note(blackboard, "Linear issue summary comment added.")
        if terminal_failure:
            _record_linear_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="Linear finalize sync could not be fully dispatched from the outbox.",
            )
        if blackboard is not None:
            _append_blackboard_note(blackboard, "Linear sync enqueued through the coordination outbox.")
            save_blackboard(layout["blackboard"], blackboard)
            write_blackboard_snapshot(layout["run_dir"], blackboard)
        if scope != "always":
            _disconnect_linear_for_coding(
                cfg=cfg,
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                event_type="linear_mcp.disconnected_after_finalize",
            )
        return terminal_failure
    if not _connect_linear_for_phase(
        cfg=cfg,
        run_id=run_id,
        layout=layout,
        status=status,
        blackboard=blackboard,
        event_type="linear_mcp.connected_for_finalize",
        required=False,
    ):
        return False
    warning = linear_update_issue(cfg, task, _linear_update_fields_for_status(cfg, target_status, labels))
    if warning:
        _record_linear_warning(run_id=run_id, layout=layout, status=status, blackboard=blackboard, message=warning)
        return False
    if remote_sync in {"status_comment", "rich"}:
        comment_body = build_linear_comment_body(
            run_id=run_id,
            task_title=task.get("title") or "Linear task",
            outcome=outcome,
            summary=summary,
            diff_snapshot=diff_snapshot,
            review_returncode=review_returncode,
            test_returncode=test_returncode,
        )
        comment_warning = linear_add_comment(cfg, task, comment_body)
        if comment_warning:
            _record_linear_warning(run_id=run_id, layout=layout, status=status, blackboard=blackboard, message=comment_warning)
    append_event(layout["events"], "linear_issue.status_updated", run_id, {"status": target_status})
    if blackboard is not None:
        _append_blackboard_note(blackboard, f"Linear issue status updated to `{target_status}`.")
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    if scope != "always":
        _disconnect_linear_for_coding(
            cfg=cfg,
            run_id=run_id,
            layout=layout,
            status=status,
            blackboard=blackboard,
            event_type="linear_mcp.disconnected_after_finalize",
        )
    return False


def _finalize_github_sync(
    *,
    cfg: ResolvedConfig,
    task: dict[str, Any],
    run_id: str,
    layout: dict[str, Path],
    status: dict[str, Any],
    blackboard: dict[str, Any] | None,
    outcome: str,
    summary: str,
    diff_snapshot: str | None = None,
    review_returncode: int | None = None,
    test_returncode: int | None = None,
    coordination: CoordinationStore | None = None,
) -> bool:
    """Enqueue + dispatch GitHub finalize-status / comment outbox events.

    Returns True if a terminal outbox failure occurred. Callers that complete
    a successful run (outcome="completed") should treat True as a hard error
    and block the run with kind="github_sync_failed" — otherwise the operator
    sees a green run while the GitHub board still shows In progress.

    Non-ship callers (outcome != "completed") can ignore the return value:
    a terminal sync failure on a blocked run is not interesting because the
    task is already in a non-completion state.
    """
    source_type = str((task.get("source") or {}).get("type") or cfg.task_source.type)
    if source_type == "linear":
        return _finalize_linear_sync(
            cfg=cfg,
            task=task,
            run_id=run_id,
            layout=layout,
            status=status,
            blackboard=blackboard,
            outcome=outcome,
            summary=summary,
            diff_snapshot=diff_snapshot,
            review_returncode=review_returncode,
            test_returncode=test_returncode,
            coordination=coordination,
        )
    remote_sync = github_remote_sync_mode(cfg, source_type)
    scope = github_mcp_scope(cfg, source_type)
    if remote_sync == "off" or scope not in {"intake_finalize", "always"}:
        return False
    if coordination is not None:
        target_status = github_project_status_name_for_outcome(outcome)
        coordination.enqueue_outbox(
            kind="github_project.status_update",
            aggregate_type="task",
            aggregate_id=str(task.get("task_id") or run_id),
            payload={
                "run_id": run_id,
                "outcome": outcome,
                "summary": summary,
                "target_status": target_status,
                "task": task,
            },
            dedupe_key=f"{run_id}:github:finalize-status",
        )
        if remote_sync == "status_comment":
            comment_body = build_issue_comment_body(
                run_id=run_id,
                task_title=task.get("title") or "GitHub task",
                outcome=outcome,
                summary=summary,
                diff_snapshot=diff_snapshot,
                review_returncode=review_returncode,
                test_returncode=test_returncode,
            )
            coordination.enqueue_outbox(
                kind="github_issue.comment",
                aggregate_type="task",
                aggregate_id=str(task.get("task_id") or run_id),
                payload={
                    "run_id": run_id,
                    "outcome": outcome,
                    "summary": summary,
                    "diff_snapshot": diff_snapshot,
                    "review_returncode": review_returncode,
                    "test_returncode": test_returncode,
                    "body": comment_body,
                    "task": task,
                },
                dedupe_key=f"{run_id}:github:finalize-comment",
            )
        summary_result = _dispatch_outbox_now(cfg, coordination, limit=25)
        terminal_failure = False
        for result in summary_result.get("items") or []:
            payload = dict(result.get("payload") or {})
            if str(payload.get("run_id") or "") != run_id:
                continue
            kind = str(result.get("kind") or "").strip()
            if str(result.get("status") or "").strip().lower() != "dispatched":
                terminal_failure = terminal_failure or bool(result.get("terminal"))
                continue
            if kind == "github_project.status_update" and str(result.get("status") or "").strip().lower() == "dispatched":
                target_status = payload.get("target_status") or github_project_status_name_for_outcome(outcome)
                append_event(layout["events"], "github_project.status_updated", run_id, {"status": target_status})
                source = task.get("source") or {}
                owner = str(source.get("owner") or "").strip()
                project = source.get("project")
                if owner and project not in (None, ""):
                    try:
                        invalidate_cached_github_project_board_snapshot(cfg, owner, int(project))
                    except Exception:
                        logger.debug("Failed to invalidate GitHub project board snapshot", exc_info=True)
                if blackboard is not None:
                    _append_blackboard_note(blackboard, f"GitHub Project status updated to `{target_status}`.")
            elif kind == "github_issue.comment":
                append_event(layout["events"], "github_project.comment_added", run_id, {"outcome": outcome})
                if blackboard is not None:
                    _append_blackboard_note(blackboard, "GitHub issue summary comment added.")
        if terminal_failure:
            _record_github_warning(
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                message="GitHub finalize sync could not be fully dispatched from the outbox.",
            )
        if blackboard is not None:
            _append_blackboard_note(blackboard, "GitHub sync enqueued through the coordination outbox.")
            save_blackboard(layout["blackboard"], blackboard)
            write_blackboard_snapshot(layout["run_dir"], blackboard)
        if scope != "always":
            _disconnect_github_for_coding(
                cfg=cfg,
                run_id=run_id,
                layout=layout,
                status=status,
                blackboard=blackboard,
                event_type="github_mcp.disconnected_after_finalize",
            )
        return terminal_failure
    if not _connect_github_for_phase(
        cfg=cfg,
        run_id=run_id,
        layout=layout,
        status=status,
        blackboard=blackboard,
        event_type="github_mcp.connected_for_finalize",
        required=False,
    ):
        return False
    if blackboard is not None:
        save_blackboard(layout["blackboard"], blackboard)
        write_blackboard_snapshot(layout["run_dir"], blackboard)
    if scope != "always":
        _disconnect_github_for_coding(
            cfg=cfg,
            run_id=run_id,
            layout=layout,
            status=status,
            blackboard=blackboard,
            event_type="github_mcp.disconnected_after_finalize",
        )
    return False


def _role_provider_override_config(
    *,
    cfg: ResolvedConfig,
    layout: dict[str, Path],
    role: str,
    provider: str,
    model: str,
) -> Path | None:
    artifacts_dir = layout["run_dir"] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return write_provider_override_config(
        cfg=cfg,
        provider=provider,
        model=model,
        output_path=artifacts_dir / f"{role}-provider-config.json",
    )


def _permission_requests_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("permissions", "requests"):
        raw_items = payload.get(key) or []
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            request_id = str(raw_item.get("request_id") or raw_item.get("id") or "")
            dedupe_key = request_id or str(id(raw_item))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(raw_item)
    return items


def _auto_approve_loop(cfg: ResolvedConfig, stop_event: threading.Event) -> None:
    """Background thread to auto-approve Tandem permissions and agent spawn requests."""
    seen_approvals: set[str] = set()
    seen_permissions: set[str] = set()

    while not stop_event.is_set():
        try:
            # 1. Handle spawn approvals
            approvals_payload = sdk_agent_teams_list_approvals(cfg)
            items = (approvals_payload.get("approvals") or []) if isinstance(approvals_payload, dict) else []
            for ap in items:
                ap_id = str(ap.get("approval_id") or ap.get("id") or "")
                status = str(ap.get("status") or "").strip().lower()
                if ap_id and status == "pending" and ap_id not in seen_approvals:
                    try:
                        sdk_agent_teams_approve_spawn(cfg, ap_id, reason="ACA auto-approve spawn")
                        seen_approvals.add(ap_id)
                    except Exception:
                        logger.warning("Failed to auto-approve spawn %s", ap_id, exc_info=True)

            # 2. Handle general permissions (bash, write, etc)
            permissions_payload = list_engine_permissions(cfg)
            for perm in _permission_requests_from_payload(permissions_payload):
                request_id = str(perm.get("request_id") or perm.get("id") or "")
                status = str(perm.get("status") or "").strip().lower()
                if request_id and status == "pending" and request_id not in seen_permissions:
                    try:
                        reply_engine_permission(cfg, request_id, "allow")
                        seen_permissions.add(request_id)
                    except Exception:
                        logger.warning("Failed to auto-approve permission %s", request_id, exc_info=True)
        except Exception:
            logger.debug("Auto-approve loop tick failed", exc_info=True)
        time.sleep(1.0)


def run_qa(cfg: ResolvedConfig, pr_number: int) -> dict[str, Any]:
    """Specialized run mode to audit an existing Pull Request."""
    run_id = cfg.env.get("ACA_RUN_ID") or new_run_id(prefix="qa")
    output_root = cfg.output_root()
    run_dir = output_root / run_id
    configure_artifact_store_root(cfg.artifact_store_root())
    layout = ensure_layout(run_dir)
    
    append_event(layout["events"], "qa.started", run_id, {"pr_number": pr_number})
    
    # 1. Resolve Repo
    repo = resolve_repository(cfg)
    repo_path = Path(repo["path"])
    
    # 2. Fetch PR Info via GitHub MCP
    ensure_github_mcp_connected(cfg)
    slug = cfg.repository.slug
    owner, repo_name = slug.split("/", 1)
    pr_info = get_pull_request(cfg, owner, repo_name, pr_number)
    
    head_branch = pr_info["head"]["ref"]
    append_event(layout["events"], "qa.pr_fetched", run_id, {"branch": head_branch, "title": pr_info["title"]})
    
    # 3. Checkout PR Branch
    run_command(["git", "-C", str(repo_path), "fetch", "origin", head_branch], env=cfg.env)
    run_command(["git", "-C", str(repo_path), "checkout", head_branch], env=cfg.env)
    
    # 4. Get Diff against Base
    base_branch = pr_info["base"]["ref"]
    diff_result = run_command(["git", "-C", str(repo_path), "diff", f"origin/{base_branch}...HEAD"], env=cfg.env)
    diff_text = diff_result.stdout
    
    # 5. Execute QA Agent
    qa_prompt = build_qa_prompt(
        run_id=run_id,
        task={"title": pr_info["title"], "description": pr_info["body"], "acceptance_criteria": []},
        pr_info=pr_info,
        diff=diff_text
    )
    
    qa_model_selection = engine_session_provider_model(cfg, "reviewer")
    qa_cli_provider = qa_model_selection["provider"]
    qa_model = qa_model_selection["model"]
    
    result = stream_tandem_prompt(
        cfg,
        role="qa-agent",
        prompt=qa_prompt,
        cwd=repo_path,
        provider=qa_cli_provider,
        model=qa_model,
        env=engine_env(cfg),
        log_path=layout["logs"] / "qa-agent.log",
        require_tool_use=True,
    )
    
    # Sync artifacts (like browser screenshots)
    sync_worker_artifacts(repo_path, layout["artifacts"], run_id, "qa-agent", layout["events"])
    
    # 6. Finalize
    status = initial_status(run_id, {"title": f"QA Audit: PR #{pr_number}"}, repo, {"version": "qa"}, {"id": qa_provider, "model": qa_model}, {}, run_dir)
    status["run"]["status"] = "completed" if result["returncode"] == 0 else "failed"
    write_status(layout["status"], status)
    
    blackboard = initial_blackboard(run_id, {"title": f"QA Audit: PR #{pr_number}"}, repo, {}, {}, {})
    blackboard["qa_result"] = result["stdout"]
    save_blackboard(layout["blackboard"], blackboard)
    
    append_event(layout["events"], "qa.completed", run_id, {"returncode": result["returncode"]})
    
    return {"run_id": run_id, "status": status, "result": result}


def run_once(cfg: ResolvedConfig) -> dict[str, Any]:
    run_id = cfg.env.get("ACA_RUN_ID") or new_run_id()
    output_root = cfg.output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    configure_artifact_store_root(cfg.artifact_store_root())
    layout = ensure_layout(run_dir)
    coordination = _coordination_store(cfg)

    append_event(layout["events"], "run.started", run_id, {"run_dir": str(run_dir)})

    engine, blocked = check_engine_at_startup(cfg, run_id, run_dir, layout)
    if blocked is not None:
        return blocked

    # Start background auto-approval loop
    stop_approvals = threading.Event()
    approval_thread = threading.Thread(
        target=_auto_approve_loop,
        args=(cfg, stop_approvals),
        daemon=True,
    )
    approval_thread.start()

    shutdown_handler = ShutdownHandler()
    shutdown_handler.hook()

    try:
        return _run_once_internal(cfg, run_id, run_dir, layout, coordination)
    finally:
        shutdown_handler.unhook()
        stop_approvals.set()
        approval_thread.join(timeout=2.0)


def _run_once_internal(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    coordination: CoordinationStore,
) -> dict[str, Any]:
    """Wrapper around _run_once_internal_impl that guarantees:
      1. The coordination lease is released on every exit path (success,
         blocked, or uncaught exception). Without this, a crash leaves the
         lease alive until TTL expires and blocks any other worker from
         picking up the same task.
      2. Uncaught exceptions get logged with structured context and produce
         a standard blocked-run result instead of bubbling out of run_once.
    """
    refs: dict[str, Any] = {"ctx": None}
    crashed_exc: Exception | None = None
    result: dict[str, Any] | None = None

    try:
        result = _run_once_internal_impl(cfg, run_id, run_dir, layout, coordination, refs)
        return result
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        crashed_exc = exc
        ctx_local = refs.get("ctx")
        phase_str = "unknown"
        if ctx_local and getattr(ctx_local, "status", None):
            phase_str = str(ctx_local.status.get("phase") or "unknown")
        logger.exception(
            "Unhandled exception in run_once (run_id=%s, phase=%s, lease_id=%s)",
            run_id,
            phase_str,
            getattr(ctx_local, "lease_id", None),
        )
        try:
            return block_run(
                run_id=run_id,
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task=getattr(ctx_local, "task", None) if ctx_local else None,
                repo=getattr(ctx_local, "repo", None) if ctx_local else None,
                engine=getattr(ctx_local, "engine", {}) if ctx_local else {},
                phase=phase_str,
                kind="internal_error",
                message=f"Unhandled exception: {exc}",
                phase_detail=str(exc),
                coordination=coordination,
                existing_status=getattr(ctx_local, "status", None) if ctx_local else None,
            )
        except Exception:
            logger.exception("Failed to write blocked-on-crash status (run_id=%s)", run_id)
            return {
                "run_id": run_id,
                "status": {
                    "run_status": "blocked",
                    "blocker": {"kind": "internal_error", "message": str(exc)},
                },
                "layout": {k: str(v) for k, v in layout.items()},
            }
    finally:
        ctx_final = refs.get("ctx")
        if ctx_final is not None and getattr(ctx_final, "lease_id", None):
            try:
                release_status, release_reason = _final_lease_release_decision(
                    ctx_final,
                    layout=layout,
                    crashed_exc=crashed_exc,
                    result=result,
                )
                ctx_final.coordination.release_lease(
                    str(ctx_final.lease_id),
                    status=release_status,
                    reason=release_reason,
                )
            except Exception:
                logger.exception(
                    "Failed to release lease %s in finally (run_id=%s)",
                    ctx_final.lease_id,
                    run_id,
                )


def _status_run_value(status: Any) -> str:
    if not isinstance(status, dict):
        return ""
    direct = str(status.get("run_status") or "").strip().lower()
    if direct:
        return direct
    run = status.get("run")
    if isinstance(run, dict):
        return str(run.get("status") or "").strip().lower()
    return ""


def _status_blocker_reason(status: Any) -> str:
    if not isinstance(status, dict):
        return ""
    blocker = status.get("blocker")
    if isinstance(blocker, dict) and blocker.get("active") is True:
        for key in ("detail", "message"):
            value = str(blocker.get(key) or "").strip()
            if value:
                return value
    phase = status.get("phase")
    if isinstance(phase, dict):
        value = str(phase.get("detail") or "").strip()
        if value:
            return value
    if isinstance(blocker, dict) and blocker.get("active") is True:
        return str(blocker.get("kind") or "").strip()
    return ""


def _status_has_active_blocker(status: Any) -> bool:
    return isinstance(status, dict) and isinstance(status.get("blocker"), dict) and status["blocker"].get("active") is True


def _status_payloads_for_final_release(ctx_final: Any, layout: dict[str, Path], result: dict[str, Any] | None) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(result, dict) and isinstance(result.get("status"), dict):
        payloads.append(result["status"])
    status_path = None
    ctx_layout = getattr(ctx_final, "layout", None)
    if isinstance(ctx_layout, dict):
        status_path = ctx_layout.get("status")
    status_path = status_path or layout.get("status")
    if status_path:
        try:
            persisted = load_status(Path(status_path))
            if isinstance(persisted, dict) and persisted:
                payloads.append(persisted)
        except Exception:
            logger.debug("Failed to load persisted run status for final lease release", exc_info=True)
    ctx_status = getattr(ctx_final, "status", None)
    if isinstance(ctx_status, dict):
        payloads.append(ctx_status)
    return payloads


def _final_lease_release_decision(
    ctx_final: Any,
    *,
    layout: dict[str, Path],
    crashed_exc: Exception | None,
    result: dict[str, Any] | None,
) -> tuple[str, str]:
    if crashed_exc is not None:
        return "failed", f"crashed: {crashed_exc}"

    payloads = _status_payloads_for_final_release(ctx_final, layout, result)
    for payload in payloads:
        if _status_has_active_blocker(payload):
            return "blocked", _status_blocker_reason(payload) or "run blocked"

    for payload in payloads:
        run_status = _status_run_value(payload)
        if run_status == "completed":
            return "completed", "run completed"
        if run_status == "blocked":
            return "blocked", _status_blocker_reason(payload) or "run blocked"
        if run_status == "failed":
            return "failed", _status_blocker_reason(payload) or "run failed"
        if run_status == "cancelled":
            return "blocked", _status_blocker_reason(payload) or "run cancelled"

    return "blocked", "run finished without terminal status"


def _run_once_internal_impl(
    cfg: ResolvedConfig,
    run_id: str,
    run_dir: Path,
    layout: dict[str, Path],
    coordination: CoordinationStore,
    refs: dict[str, Any],
) -> dict[str, Any]:
    """Thin orchestrator for a single ACA coding run.

    Each logical phase is handled by a dedicated phase module in
    ``src/tandem_agents/core/phases/``.  The RunContext object carries all shared
    mutable state so phase functions have clean single-argument signatures.

    Phase order
    -----------
    1. Engine health + repository binding     (engine_check)
    2. Task intake + coordination claim       (task_intake)
    3. Coder-backend fast path               (inline — short-circuits before planning)
    4. Repair loop  (max_loops iterations):
       a. Manager prompt + subtask planning  (planning)
       b. Subtask pre-screening             (planning)
       c. Worker dispatch                   (worker_dispatch)
       d. Integration prompt                (inline — runner_core private helpers)
       e. No-diff / no-proof repair check   (repair)
       f. Review + verification             (review_verify)
       g. Retry or finalize                 (repair / finalize)
    """
    # ------------------------------------------------------------------
    # Phase 1: Engine health + repository binding
    # ------------------------------------------------------------------
    engine, blocked = check_engine_health(cfg, run_id, run_dir, layout)
    if blocked is not None:
        return blocked

    # resolve_repository() called inside check_engine_health already resolves
    # and returns the repo — retrieve it via a lightweight re-ping (no disk I/O).
    repo = resolve_repository(cfg)
    append_event(layout["events"], "repo.resolved", run_id, {"path": repo["path"], "branch": repo.get("branch")})
    preflight, preflight_blocker = _run_start_preflight(cfg, run_id=run_id, layout=layout, repo=repo)
    repo["run_start_preflight"] = preflight
    if preflight_blocker is not None:
        return block_run(
            run_id=run_id,
            run_dir=run_dir,
            layout=layout,
            cfg=cfg,
            task=None,
            repo=repo,
            engine=engine,
            phase="repo_resolution",
            kind=preflight_blocker["kind"],
            message=preflight_blocker["message"],
            phase_detail=preflight_blocker["recovery_action"],
            coordination=coordination,
        )

    # ------------------------------------------------------------------
    # Phase 2: Task intake + coordination claim
    # ------------------------------------------------------------------
    ctx = _PhaseRunContext(
        run_id=run_id,
        run_dir=run_dir,
        layout=layout,
        cfg=cfg,
        coordination=coordination,
        engine=engine,
        repo=repo,
    )
    # Register ctx with the wrapper so its finally can release the lease on
    # every exit path (including uncaught exceptions). See _run_once_internal.
    refs["ctx"] = ctx

    blocked = run_task_intake(ctx)
    if blocked is not None:
        return blocked

    # GitHub sync: claim status
    sync_claim_status(ctx)

    # ------------------------------------------------------------------
    # Phase 3: Coder-backend fast path
    # ------------------------------------------------------------------
    if ctx.execution_backend == "legacy" and ctx.source_scope != "always":
        disconnect_for_coding(ctx)

    if ctx.execution_backend == "linear_comment":
        return _run_linear_comment_backend(ctx)

    if ctx.execution_backend == "github_pr_action":
        return _run_github_pr_action_backend(ctx)

    if ctx.execution_backend == "coder":
        return _run_coder_backend(ctx)

    # ------------------------------------------------------------------
    # Phase 4: Repair loop
    # ------------------------------------------------------------------
    configured_max_retries = max(0, int(getattr(cfg.swarm, "max_retries", 1) or 0))
    base_max_loops = configured_max_retries + 1
    incomplete_diff_extra_retries = _worker_incomplete_diff_extra_retries(cfg)
    max_loops = base_max_loops + incomplete_diff_extra_retries
    ctx.status["repair"] = {
        "configured_max_retries": configured_max_retries,
        "worker_incomplete_diff_extra_retries": incomplete_diff_extra_retries,
        "base_max_loops": base_max_loops,
        "max_loops": max_loops,
        "attempt": 0,
    }
    ctx.blackboard["repair"] = dict(ctx.status["repair"])
    write_status(layout["status"], ctx.status)
    save_blackboard(layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(run_dir, ctx.blackboard)
    previous_feedback: str | None = None

    for attempt in range(max_loops):
        ctx.status.setdefault("repair", {})["attempt"] = attempt + 1
        ctx.blackboard.setdefault("repair", {})["attempt"] = attempt + 1
        write_status(layout["status"], ctx.status)
        # If coordination has been lost (3+ consecutive heartbeat misses) we
        # must not continue mutating run state on a dead lease — another
        # worker may have already reclaimed the task. Block early so the
        # operator sees a clear blocker and the reaper / reclaim logic can
        # take over cleanly.
        if ctx.coordination_lost:
            _preserve_and_reset_blocked_worktree(ctx, reason="coordination_lost")
            return block_run(
                run_id=run_id,
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task=ctx.task,
                repo=ctx.repo,
                engine=ctx.engine,
                phase="coordination",
                kind="coordination_lost",
                message=(
                    f"Lost coordination lease after {ctx.consecutive_heartbeat_misses} "
                    "consecutive heartbeat misses. Another worker may have reclaimed the task."
                ),
                phase_detail="lease heartbeat repeatedly missed",
                coordination=coordination,
                existing_status=ctx.status,
            )
        if attempt > 0:
            ctx.status = set_status(
                ctx.status, layout, phase="planning",
                phase_detail=f"Retrying (attempt {attempt + 1})"
            )
            append_event(layout["events"], "run.retry", run_id, {"attempt": attempt + 1})

        # 4a. Manager prompt
        setattr(ctx, "_previous_feedback", previous_feedback)
        setattr(ctx, "_manager_fallback_required", False)
        manager_result = run_manager_prompt(ctx)
        manager_failed = manager_result["returncode"] != 0
        manager_failure_excerpt = str(manager_result.get("stdout") or "").strip()[:1000]
        if ctx.status.get("run", {}).get("status") == "blocked":
            return _block_manager_failed(ctx)
        if manager_failed:
            append_event(
                layout["events"], "manager.failed", run_id,
                {
                    "returncode": manager_result["returncode"],
                    "stdout_excerpt": manager_failure_excerpt,
                },
                task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")},
            )
            setattr(ctx, "_manager_fallback_required", True)
            ctx.blackboard["manager_fallback"] = {
                "required": True,
                "reason": manager_failure_excerpt or "manager planning returned nonzero status",
                "used": False,
            }
            _append_blackboard_note(
                ctx.blackboard,
                "Manager planning returned no usable plan; ACA will try contract-based fallback planning.",
            )
            save_blackboard(layout["blackboard"], ctx.blackboard)
            write_blackboard_snapshot(run_dir, ctx.blackboard)

        # 4b. Pre-screen subtasks
        ctx.worker_results = []
        all_pre_satisfied = pre_screen_subtasks(ctx)
        write_status(layout["status"], ctx.status)

        if manager_failed and ctx.status["run"]["status"] != "blocked":
            pending_count = len(ctx.pending_subtasks or [])
            planned_count = len(ctx.planned_subtasks or [])
            if pending_count > 0:
                ctx.blackboard["manager_fallback"] = {
                    "required": True,
                    "reason": manager_failure_excerpt or "manager planning returned nonzero status",
                    "used": True,
                    "planned_workers": planned_count,
                    "pending_workers": pending_count,
                }
                append_event(
                    layout["events"],
                    "manager.fallback",
                    run_id,
                    {
                        "reason": manager_failure_excerpt,
                        "planned_workers": planned_count,
                        "pending_workers": pending_count,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="manager",
                    repo={"path": ctx.repo.get("path")},
                )
                save_blackboard(layout["blackboard"], ctx.blackboard)
                write_blackboard_snapshot(run_dir, ctx.blackboard)
            else:
                detail = manager_failure_excerpt or "Manager planning failed"
                ctx.status = set_status(
                    ctx.status, layout, phase="planning",
                    phase_detail="manager planning failed", run_status="blocked",
                    blocker=(True, "manager", detail, "manager"),
                )
                _touch_coordination(
                    coordination, run_id=run_id, lease_id=ctx.lease_id,
                    lease_ttl_seconds=cfg.coordination.lease_ttl_seconds,
                    status="blocked", phase="planning", error=detail,
                )
                write_status(layout["status"], ctx.status)

        if not ctx.planned_subtasks and not any(s.get("files") for s in (ctx.planned_subtasks or [])):
            return _block_no_targets(ctx)

        if ctx.status["run"]["status"] == "blocked":
            return _block_manager_failed(ctx)

        # 4c. Early-exit if everything already satisfied
        if all_pre_satisfied:
            return _complete_pre_satisfied(ctx)

        pr_context_blocked = _prepare_pr_candidate_context(ctx)
        if pr_context_blocked is not None:
            return pr_context_blocked

        # 4d. Worker dispatch
        ctx.status["metrics"]["planned_workers"] = len(ctx.planned_subtasks)
        ctx.status["metrics"].setdefault("skipped_workers", 0)
        ctx.status["metrics"].setdefault("tolerated_workers", 0)
        write_status(layout["status"], ctx.status)

        dispatch_workers(ctx)

        if ctx.coordination_lost:
            _preserve_and_reset_blocked_worktree(ctx, reason="coordination_lost")
            return block_run(
                run_id=run_id,
                run_dir=run_dir,
                layout=layout,
                cfg=cfg,
                task=ctx.task,
                repo=ctx.repo,
                engine=ctx.engine,
                phase="coordination",
                kind="coordination_lost",
                message=(
                    f"Lost coordination lease during worker execution after "
                    f"{ctx.consecutive_heartbeat_misses} consecutive heartbeat misses."
                ),
                phase_detail="lease heartbeat repeatedly missed during worker execution",
                coordination=coordination,
                existing_status=ctx.status,
            )

        if ctx.status["metrics"]["failed_workers"]:
            source = ctx.task.get("source") if isinstance(ctx.task, dict) else {}
            worker_blocker = _worker_failure_blocker(ctx.worker_results)
            retry_feedback = _worker_failure_retry_feedback(ctx, worker_blocker, attempt)
            if retry_feedback and _worker_failure_can_retry(cfg, worker_blocker, attempt, base_max_loops):
                previous_feedback = retry_feedback
                partial_diff_artifacts = _partial_diff_artifacts_for_retry(ctx.worker_results)
                completed_subtask_ids = _completed_subtask_ids_for_retry(ctx.worker_results)
                if partial_diff_artifacts:
                    ctx.blackboard.setdefault("repair", {})["partial_diff_artifacts"] = partial_diff_artifacts
                    ctx.blackboard.setdefault("repair", {})["partial_diff_state"] = "preserved_not_accepted"
                    ctx.status.setdefault("repair", {})["partial_diff_artifacts"] = partial_diff_artifacts
                    ctx.status.setdefault("repair", {})["partial_diff_state"] = "preserved_not_accepted"
                if completed_subtask_ids:
                    ctx.blackboard.setdefault("repair", {})["completed_subtask_ids"] = completed_subtask_ids
                    ctx.status.setdefault("repair", {})["completed_subtask_ids"] = completed_subtask_ids
                if worker_blocker["kind"] == "worker_incomplete_diff":
                    for repair_state in (
                        ctx.blackboard.setdefault("repair", {}),
                        ctx.status.setdefault("repair", {}),
                    ):
                        repair_state["extra_retry_source"] = "worker_incomplete_diff"
                        sources = repair_state.setdefault("extra_retry_sources", [])
                        if isinstance(sources, list) and "worker_incomplete_diff" not in sources:
                            sources.append("worker_incomplete_diff")
                _append_blackboard_note(
                    ctx.blackboard,
                    f"Attempt {attempt + 1} hit retryable worker blocker `{worker_blocker['kind']}`. Retrying.",
                )
                append_event(
                    layout["events"],
                    "worker.retry_deferred_to_repair_loop",
                    run_id,
                    {
                        "attempt": attempt + 1,
                        "kind": worker_blocker["kind"],
                        "detail": worker_blocker.get("detail"),
                        "partial_diff_artifacts": partial_diff_artifacts,
                        "completed_subtask_ids": completed_subtask_ids,
                    },
                    task_id=ctx.task.get("task_id"),
                    role="worker",
                    repo={"path": ctx.repo.get("path")},
                )
                save_blackboard(layout["blackboard"], ctx.blackboard)
                write_blackboard_snapshot(run_dir, ctx.blackboard)
                continue
            if _has_unresolved_write_required_worker_failure(ctx.worker_results):
                return _block_worker_failure(ctx)
            if isinstance(source, dict) and str(source.get("type") or "").strip() == "github_project":
                return _block_worker_failure(ctx)
            repo_blocker = _repo_validation_blocker_message(ctx.repo_validation)
            if repo_blocker:
                return _block_worker_failure(ctx)
            _append_blackboard_note(
                ctx.blackboard,
                "Worker failures were tolerated because the expected repository files were present after sync.",
            )
            save_blackboard(layout["blackboard"], ctx.blackboard)
            write_blackboard_snapshot(run_dir, ctx.blackboard)

        repo_blocker = _repo_validation_blocker_message(ctx.repo_validation)
        if repo_blocker:
            missing_files = [
                str(path)
                for path in (ctx.repo_validation.get("missing_files") or [])
                if str(path).strip()
            ]
            command_failures = ctx.repo_validation.get("command_failures") or []
            feedback_parts = [
                "CRITICAL: Deterministic repository validation failed after worker sync.",
                repo_blocker,
            ]
            if missing_files:
                feedback_parts.append(
                    "Missing expected files:\n"
                    + "\n".join(f"- {path}" for path in missing_files)
                )
            if command_failures:
                feedback_parts.append(
                    "Verification command failures:\n"
                    + json.dumps(command_failures, indent=2, default=str)[:4000]
                )
            feedback_parts.append(
                "Preserve any useful existing diff, then add the missing tracked source, test, and documentation files. "
                "Do not proceed to review until deterministic validation passes."
            )
            repo_feedback = "\n\n".join(feedback_parts)
            _append_blackboard_note(
                ctx.blackboard,
                f"Attempt {attempt + 1} failed deterministic repo validation: {repo_blocker}",
            )
            append_event(
                layout["events"],
                "repo_validation.failed",
                run_id,
                {
                    "reason": repo_blocker,
                    "missing_files": missing_files,
                    "will_retry": attempt < base_max_loops - 1,
                },
                task_id=ctx.task.get("task_id"),
                role="manager",
                repo={"path": ctx.repo.get("path")},
            )
            save_blackboard(layout["blackboard"], ctx.blackboard)
            write_blackboard_snapshot(run_dir, ctx.blackboard)
            if attempt < base_max_loops - 1:
                previous_feedback = repo_feedback
                continue
            return _block_from_decision(
                ctx,
                RepairDecision(
                    action="block",
                    message=repo_blocker,
                    kind="repo_validation_failed",
                    phase="handoff",
                ),
            )

        # 4e. Integration prompt  (still inline — small and tightly coupled to result handling)
        integration_result = _run_integration_prompt(ctx)
        append_event(
            layout["events"],
            "manager.completed" if integration_result["returncode"] == 0 else "manager.failed",
            run_id, {"stage": "integration", "returncode": integration_result["returncode"]},
            task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")},
        )

        if integration_result["returncode"] != 0:
            if _integration_failure_can_defer_to_review(integration_result):
                _append_blackboard_note(
                    ctx.blackboard,
                    "Integration prompt hit an engine watchdog after worker sync; deferring to review and deterministic verification.",
                )
                append_event(
                    layout["events"],
                    "integration.deferred",
                    run_id,
                    {"reason": str(integration_result.get("stdout") or "").strip()[:500]},
                    task_id=ctx.task.get("task_id"),
                    role="manager",
                    repo={"path": ctx.repo.get("path")},
                )
                save_blackboard(layout["blackboard"], ctx.blackboard)
                write_blackboard_snapshot(run_dir, ctx.blackboard)
            else:
                return _block_integration_failed(ctx)
        else:
            integration_blocker = _integration_blocker_message(integration_result)
            if integration_blocker:
                if _integration_semantic_blocker_can_defer_to_review(integration_result, integration_blocker):
                    _append_blackboard_note(
                        ctx.blackboard,
                        "Integration review could not inspect repository state because its tool environment was limited; deferring to review and deterministic verification.",
                    )
                    append_event(
                        layout["events"],
                        "integration.deferred",
                        run_id,
                        {"reason": integration_blocker[:500]},
                        task_id=ctx.task.get("task_id"),
                        role="manager",
                        repo={"path": ctx.repo.get("path")},
                    )
                    save_blackboard(layout["blackboard"], ctx.blackboard)
                    write_blackboard_snapshot(run_dir, ctx.blackboard)
                elif attempt < base_max_loops - 1:
                    previous_feedback = _integration_retry_feedback(ctx, attempt, integration_blocker, integration_result)
                    continue
                return _block_integration_failed(ctx, integration_blocker)

        # 4f. No-diff / no-proof repair check
        ctx.pending_diff_snapshot = git_diff_stat(ctx.repo_path)
        if not ctx.pending_diff_snapshot.strip() and not ctx.repo_validation.get("ok"):
            decision = check_no_diff(ctx, attempt, base_max_loops)
            if decision.action == "retry":
                previous_feedback = decision.feedback
                continue
            return _block_from_decision(ctx, decision)

        if not ctx.pending_diff_snapshot.strip() and ctx.repo_validation.get("ok"):
            decision = check_no_verifiable_proof(ctx, attempt, base_max_loops)
            if decision.action == "retry":
                previous_feedback = decision.feedback
                continue
            if decision.action == "block":
                return _block_from_decision(ctx, decision)
            # "continue" => fall through to review

        # 4g. Review + verification
        verification = run_review_and_test(ctx)

        if verification.should_retry:
            if _verification_can_retry(cfg, ctx, attempt, base_max_loops):
                previous_feedback = build_retry_feedback(ctx, attempt, verification)
                continue
            return _block_verification_failed(ctx, verification)
        if getattr(verification, "outcome", "pass") != "pass":
            return _block_verification_failed(ctx, verification)

        # 4h. Finalize (happy path)
        return finalize_completed_run(ctx)

    # Should not reach here — loop always returns
    return _block_from_decision(
        ctx,
        RepairDecision(
            action="block",
            message="Run exceeded maximum retry loop count without completing.",
            kind="max_retries",
            phase="handoff",
        ),
    )




# ---------------------------------------------------------------------------
# _run_once_internal helper functions
# These support the thin orchestrator above. Each extracts one cohesive
# terminal path or sub-step so the main loop stays readable.
# ---------------------------------------------------------------------------

def _run_integration_prompt(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Run the integration prompt and return the raw stream result."""
    integration_prompt = build_integration_prompt(ctx.run_id, ctx.task, ctx.worker_results)
    integration_model_selection = engine_session_provider_model(ctx.cfg, "manager")
    integration_cli_provider = integration_model_selection["provider"]
    integration_model = integration_model_selection["model"]
    _role_provider_override_config(
        cfg=ctx.cfg,
        layout=ctx.layout,
        role="integration",
        provider=integration_cli_provider,
        model=integration_model,
    )
    with _coordination_heartbeat(ctx, phase="integration"):
        return stream_tandem_prompt(
            ctx.cfg,
            role="integration",
            prompt=integration_prompt,
            cwd=ctx.repo_path,
            provider=integration_cli_provider,
            model=integration_model,
            env=engine_env(ctx.cfg),
            log_path=ctx.layout["logs"] / "manager-integration.log",
            config_path=None,
        )


def _integration_blocker_message(integration_result: dict[str, Any]) -> str | None:
    payload = _extract_json(str(integration_result.get("stdout") or "")) or {}
    if not payload:
        return None
    status = _normalized_text(payload.get("status") or payload.get("outcome") or payload.get("result"))
    next_action = _normalized_text(payload.get("next_action") or payload.get("action"))
    approved = payload.get("approved")
    blockers = payload.get("blockers")
    required_fixes = payload.get("required_fixes")
    details = _integration_payload_details(payload)
    if approved is False:
        return "Integration review did not approve the worker result." + details
    if status in {"blocked", "failed", "fail", "repair_needed", "needs_changes", "incomplete"}:
        return f"Integration review reported `{status}`." + details
    if next_action in {"blocked", "repair_needed", "needs_changes", "retry", "fix_required"}:
        return f"Integration review requested `{next_action}`." + details
    if blockers:
        return "Integration review reported blockers." + details
    if required_fixes:
        return "Integration review reported required fixes." + details
    return None


def _integration_failure_can_defer_to_review(integration_result: dict[str, Any]) -> bool:
    if int(integration_result.get("returncode") or 0) == 0:
        return False
    engine = integration_result.get("engine") if isinstance(integration_result.get("engine"), dict) else {}
    combined = " ".join(
        str(value or "")
        for value in (
            integration_result.get("stdout"),
            integration_result.get("failure_reason"),
            integration_result.get("blocker_kind"),
            engine.get("stream_reason"),
        )
    )
    markers = (
        "ENGINE_TOOL_LOOP_STALLED",
        "ENGINE_PROMPT_TIMEOUT",
        "ENGINE_EMPTY_RESPONSE",
        "engine_tool_loop_stalled",
        "engine_prompt_timeout",
        "engine_empty_response",
        "no_text_timeout",
    )
    return any(marker in combined for marker in markers)


def _integration_semantic_blocker_can_defer_to_review(
    integration_result: dict[str, Any],
    blocker_message: str,
) -> bool:
    payload = _extract_json(str(integration_result.get("stdout") or "")) or {}
    combined = " ".join(
        str(value or "")
        for value in (
            integration_result.get("stdout"),
            blocker_message,
            payload.get("summary") if isinstance(payload, dict) else "",
            payload.get("risks") if isinstance(payload, dict) else "",
            payload.get("tests") if isinstance(payload, dict) else "",
        )
    ).lower()
    concrete_worker_failure_markers = (
        "placeholder",
        "requested",
        "not implemented",
        "incomplete",
        "acceptance criteria unmet",
        "unmet acceptance",
        "missing implementation",
        "missing coverage",
        "no repository diff",
        "no filesystem changes",
        "worker output only",
        "cargo test",
    )
    if any(marker in combined for marker in concrete_worker_failure_markers):
        return False
    inspection_markers = (
        "bubblewrap_not_available",
        "sandbox",
        "git/status",
        "status/diff inspection",
        "could not inspect",
        "cannot verify",
        "not run:",
        "tool environment",
        "commands were blocked",
    )
    return any(marker in combined for marker in inspection_markers)


def _integration_payload_details(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"Summary: {summary}")
    for label, key in (("Blockers", "blockers"), ("Required fixes", "required_fixes"), ("Risks", "risks"), ("Tests", "tests")):
        value = payload.get(key)
        entries: list[str] = []
        if isinstance(value, list):
            entries = [str(entry).strip() for entry in value if str(entry).strip()]
        elif isinstance(value, dict):
            entries = [f"{k}: {v}" for k, v in value.items() if str(v).strip()]
        elif str(value or "").strip():
            entries = [str(value).strip()]
        if entries:
            lines.append(f"{label}: " + "; ".join(entries[:5]))
    return ("\n" + "\n".join(lines)) if lines else ""


def _integration_retry_feedback(
    ctx: "_PhaseRunContext",
    attempt: int,
    blocker_message: str,
    integration_result: dict[str, Any],
) -> str:
    from src.tandem_agents.runtime.runstate import save_blackboard
    from src.tandem_agents.runtime.run_output import write_blackboard_snapshot

    stdout = str(integration_result.get("stdout") or "").strip()
    feedback = "\n\n".join(
        part
        for part in (
            "Integration review rejected the previous worker result. Repair these issues before continuing.",
            blocker_message,
            f"Raw integration output:\n{stdout}" if stdout else "",
        )
        if part
    )
    _append_blackboard_note(
        ctx.blackboard,
        f"Attempt {attempt + 1} failed integration review. Retrying with integration feedback.",
    )
    ctx.blackboard.setdefault("integration_reviews", []).append(
        {
            "attempt": attempt + 1,
            "returncode": integration_result.get("returncode"),
            "blocker": blocker_message,
            "stdout": stdout,
        }
    )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    return feedback


def _preserve_and_reset_blocked_worktree(ctx: "_PhaseRunContext", *, reason: str) -> None:
    status = run_command(_git_repo_args(ctx.repo_path, "status", "--short", "--untracked-files=all"), env=ctx.cfg.env)
    if status.returncode != 0 or not status.stdout.strip():
        return
    diff = run_command(_git_repo_args(ctx.repo_path, "diff", "--binary", "HEAD"), env=ctx.cfg.env)
    patch_path = ctx.layout["artifacts"] / "blocked-working-diff.patch"
    patch_text = diff.stdout if diff.returncode == 0 and diff.stdout.strip() else status.stdout
    patch_path.write_text(patch_text, encoding="utf-8")
    ctx.blackboard.setdefault("artifacts", []).append(str(patch_path))
    ctx.blackboard.setdefault("blocked_worktree_cleanup", []).append(
        {
            "reason": reason,
            "patch_path": str(patch_path),
            "status": status.stdout.strip(),
        }
    )
    reset = run_command(_git_repo_args(ctx.repo_path, "reset", "--hard", "HEAD"), env=ctx.cfg.env)
    clean = run_command(_git_repo_args(ctx.repo_path, "clean", "-fd"), env=ctx.cfg.env)
    if reset.returncode != 0 or clean.returncode != 0:
        _append_blackboard_note(
            ctx.blackboard,
            "Warning: failed to fully clean blocked run worktree after preserving diff artifact.",
        )
    else:
        _append_blackboard_note(
            ctx.blackboard,
            f"Preserved blocked worktree diff at `{patch_path}` and reset shared checkout.",
        )


def _linear_comment_task_summary(task: dict[str, Any]) -> str:
    body = str(task.get("raw_issue_body") or task.get("description") or "").strip()
    criteria = [str(entry).strip() for entry in (task.get("acceptance_criteria") or []) if str(entry).strip()]
    lines = body.splitlines()
    current_section = ""
    decisions: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add_decision(pr_number: str, decision: str, note: str) -> None:
        key = str(pr_number).strip().lstrip("#")
        if not key or key in seen:
            return
        seen.add(key)
        decisions.append((f"#{key}", decision, note))

    for raw_line in lines:
        line = raw_line.strip()
        lowered = line.lower()
        if not line:
            continue
        if "green-ish" in lowered or "cherry-pickable" in lowered:
            current_section = "green"
            continue
        if "large or suspicious" in lowered:
            current_section = "large"
            continue
        if "failing" in lowered:
            current_section = "failing"
            continue
        numbers = re.findall(r"#(\d+)", line)
        if not numbers:
            continue
        if current_section == "green":
            decision = "cherry-pick"
            note = "Candidate only after current-main validation; keep small and cherry-pick rather than merging directly."
        elif current_section == "large":
            decision = "needs-manual-review"
            note = "Too large or broad for automatic merge; inspect manually before taking any code."
        elif current_section == "failing":
            decision = "close"
            note = "Failing generated PR; close unless a maintainer identifies unique value for a consolidated follow-up."
        else:
            decision = "needs-manual-review"
            note = "Issue body did not provide enough status context for an automatic decision."
        for number in numbers:
            add_decision(number, decision, note)

    summary_lines = [
        "Linear-only ACA task completed.",
        "",
        "No repository changes, commit, push, or PR were expected for this task; the requested artifact is this Linear decision note.",
    ]
    if decisions:
        summary_lines.extend(["", "PR decisions:"])
        for pr_number, decision, note in decisions:
            summary_lines.append(f"- {pr_number}: {decision} - {note}")
    if criteria:
        summary_lines.extend(["", "Acceptance handled:"])
        summary_lines.extend(f"- {entry}" for entry in criteria)
    if not decisions and not criteria:
        summary_lines.extend(["", "Summary:", task.get("title") or "Linear task reviewed."])
    return "\n".join(summary_lines).strip()


def _run_linear_comment_backend(ctx: "_PhaseRunContext") -> dict[str, Any]:
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.run_output import save_run_text, write_board_snapshot
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard

    summary = _linear_comment_task_summary(ctx.task)
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="linear_comment",
        phase_detail="posting Linear decision note",
        phase_role="manager",
        run_status="running",
        metrics={
            "planned_workers": 0,
            "completed_workers": 0,
            "failed_workers": 0,
            "skipped_workers": 0,
            "tolerated_workers": 0,
            "tests_passed": True,
        },
    )
    _touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running",
        phase="linear_comment",
        ctx=ctx,
    )
    append_event(
        ctx.layout["events"],
        "linear_comment.started",
        ctx.run_id,
        {"execution_backend": "linear_comment"},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    ctx.blackboard["linear_comment_summary"] = summary
    ctx.blackboard["workers"] = []
    ctx.blackboard["review"] = {"role": "reviewer", "returncode": 0, "stdout": "Linear-only task; no code diff to review."}
    ctx.blackboard["test"] = {"role": "tester", "returncode": 0, "stdout": "Linear-only task; no repository tests required."}
    _append_blackboard_note(ctx.blackboard, "Linear-only task classified as a decision/comment update; skipped repo workers.")

    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="handoff",
        phase_detail="linear decision note posted",
        phase_role="manager",
        run_status="completed",
        run_completed=True,
        metrics={
            "planned_workers": 0,
            "completed_workers": 0,
            "failed_workers": 0,
            "skipped_workers": 0,
            "tolerated_workers": 0,
            "tests_passed": True,
        },
    )
    _touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="completed",
        phase="handoff",
        completed=True,
    )
    task_key, lease_id, worker_id, host_id, _ = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id:
        ctx.coordination.mark_task_done(
            task_key,
            run_id=ctx.run_id,
            lease_id=lease_id,
            worker_id=worker_id,
            host_id=host_id,
            reason="linear decision note posted",
        )
    _move_task_card_if_present(ctx.board, ctx.task, "review", "manager", "linear decision note posted")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    save_run_text(
        ctx.layout["summary"],
        "\n".join(
            [
                "# Run completed",
                "",
                f"- Run ID: `{ctx.run_id}`",
                f"- Task: {ctx.task.get('title') or 'Linear task'}",
                "- Execution backend: `linear_comment`",
                "",
                summary,
            ]
        ).strip()
        + "\n",
    )
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    sync_failed = _finalize_github_sync(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        outcome="completed",
        summary=summary,
        diff_snapshot="(linear-only task; no repository diff)",
        review_returncode=0,
        test_returncode=0,
        coordination=ctx.coordination,
    )
    if sync_failed:
        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=ctx.task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="linear_sync",
            kind="linear_sync_failed",
            message="Linear decision note could not be dispatched.",
            phase_detail="Linear finalize sync hit terminal outbox failure",
            coordination=ctx.coordination,
            existing_status=ctx.status,
        )
    append_event(
        ctx.layout["events"],
        "run.completed",
        ctx.run_id,
        {"result": "completed", "execution_backend": "linear_comment"},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    return ctx.make_result(worker_results=[], board=ctx.board)


def _run_github_pr_action_backend(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Create approval-gated GitHub PR actions through the external-action runner."""
    message = (
        "GitHub PR actions were proposed and are waiting for operator approval. "
        "No GitHub write will execute until approval is granted."
    )
    pr_contexts = fetch_pr_contexts(ctx.cfg, ctx.task)
    actions = default_action_plan(ctx.run_id, ctx.task, pr_contexts)
    approvals = enqueue_approvals_for_plan(
        ctx.coordination,
        run_id=ctx.run_id,
        task=ctx.task,
        actions=actions,
    )
    pending_count = len([row for row in approvals if row.get("status") == "pending"])
    ctx.blackboard["external_action"] = {
        "execution_backend": "github_pr_action",
        "adapter": "github_pr",
        "pr_contexts": pr_contexts,
        "proposed_actions": actions,
        "approvals": approvals,
    }
    ctx.status["metrics"]["planned_external_actions"] = len(actions)
    ctx.status["metrics"]["pending_external_approvals"] = pending_count
    _append_blackboard_note(ctx.blackboard, message)
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(
        ctx.layout["summary"],
        build_blocked_summary(
            task_title=ctx.task["title"],
            message=f"{message}\n\nPending approvals: {pending_count}.",
        ),
    )
    append_event(
        ctx.layout["events"],
        "github_pr_action.approvals_pending",
        ctx.run_id,
        {"execution_backend": "github_pr_action", "approval_count": len(approvals), "pending_count": pending_count},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )
    result = block_run(
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        layout=ctx.layout,
        cfg=ctx.cfg,
        task=ctx.task,
        repo=ctx.repo,
        engine=ctx.engine,
        phase="github_pr_action",
        kind="pending_approval",
        message=message,
        phase_detail="GitHub PR actions are waiting for approval",
        coordination=ctx.coordination,
        existing_status=ctx.status,
    )
    _finalize_github_sync(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=result["status"],
        blackboard=ctx.blackboard,
        outcome="blocked",
        summary=message,
        coordination=ctx.coordination,
    )
    return result


def _run_coder_backend(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Handle the coder-backend execution path (fast-exit branch)."""
    from src.tandem_agents.core.engine.coder_backend import coder_workflow_supported, execute_coder_run
    from src.tandem_agents.runtime.run_output import (
        build_coder_summary,
        build_blocked_summary,
        save_run_text,
        set_status,
        write_blackboard_snapshot,
        write_board_snapshot,
    )
    from src.tandem_agents.core.repository.board import save_board

    board = ctx.board
    board_path = ctx.board_path

    if not coder_workflow_supported(ctx.task, ctx.repo):
        _reason = "Coder backend only supports GitHub Project tasks backed by a linked issue."
        ctx.status = set_status(
            ctx.status, ctx.layout,
            phase="coder_execution",
            phase_detail="coder backend does not support this task shape",
            run_status="blocked",
            blocker=(True, "coder", _reason, "manager"),
            run_completed=True,
        )
        save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=_reason))
        _finalize_github_sync(
            cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
            status=ctx.status, blackboard=ctx.blackboard,
            outcome="blocked", summary=_reason, coordination=ctx.coordination,
        )
        if ctx.lease_id:
            ctx.coordination.release_lease(str(ctx.lease_id), status="blocked", reason="coder backend unsupported")
        append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "coder_unsupported"})
        return ctx.make_result()

    ctx.status = set_status(ctx.status, ctx.layout, phase="coder_execution", phase_role="worker", run_status="running")
    _touch_coordination(
        ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="running", phase="coder_execution",
        ctx=ctx,
    )
    task_key, lease_id, worker_id, host_id, lease_expires_at_ms = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id and lease_expires_at_ms is not None:
        ctx.coordination.mark_task_active(
            task_key, run_id=ctx.run_id, lease_id=lease_id,
            worker_id=worker_id, host_id=host_id,
            lease_expires_at_ms=int(lease_expires_at_ms), reason="coder execution started",
        )
    append_event(ctx.layout["events"], "coder.started", ctx.run_id,
                 {"workflow_mode": "issue_fix"}, task_id=ctx.task.get("task_id"),
                 role="manager", repo={"path": ctx.repo.get("path")})

    try:
        coder_result = execute_coder_run(ctx.cfg, run_id=ctx.run_id, repo=ctx.repo, task=ctx.task)
    except Exception as exc:
        detail = str(exc).strip() or repr(exc)
        ctx.status = set_status(
            ctx.status, ctx.layout, phase="coder_execution", phase_detail=detail,
            run_status="blocked", blocker=(True, "coder", detail, "manager"), run_completed=True,
        )
        _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                            lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                            status="blocked", phase="coder_execution", error=detail, completed=True)
        _move_task_card_if_present(board, ctx.task, "blocked", "manager", "coder execution failure")
        save_board(board_path, board)
        write_board_snapshot(ctx.run_dir, board)
        save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=detail))
        _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                              status=ctx.status, blackboard=ctx.blackboard,
                              outcome="blocked", summary=detail, coordination=ctx.coordination)
        if ctx.lease_id:
            ctx.coordination.release_lease(str(ctx.lease_id), status="blocked", reason="coder execution failed")
        append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "coder"})
        return ctx.make_result()

    ctx.blackboard["coder_run"] = coder_result.get("coder_run") or {}
    ctx.blackboard["artifacts"] = coder_result.get("artifacts") or []
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    append_event(ctx.layout["events"], "coder.completed", ctx.run_id,
                 {"status": coder_result.get("status"), "phase": coder_result.get("phase"),
                  "artifact_count": len(coder_result.get("artifacts") or [])},
                 task_id=ctx.task.get("task_id"), role="manager", repo={"path": ctx.repo.get("path")})

    apply_coder_result(
        ctx.cfg,
        ctx.coordination,
        run_id=ctx.run_id,
        coder_result=coder_result,
        status_payload=ctx.status,
        blackboard=ctx.blackboard,
    )
    ctx.status = load_status(ctx.layout["status"])
    return ctx.make_result()


def _block_no_targets(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when the manager produced no subtask targets."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    from src.tandem_agents.core.repository.board import save_board

    msg = "Manager planning produced no subtasks and ACA could not infer a credible repo target set."
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="planning",
        phase_detail="no credible repository target set could be inferred",
        run_status="blocked",
        blocker=(True, "manager", msg, "manager"),
        metrics={"planned_workers": len(ctx.planned_subtasks), "completed_workers": 0,
                 "failed_workers": 0, "skipped_workers": 0, "tolerated_workers": 0},
        run_completed=True,
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="planning", error=msg, completed=True)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "no repository target set")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=msg))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard, outcome="blocked",
                          summary=msg, coordination=ctx.coordination)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "no_targets"})
    return ctx.make_result()


def _block_manager_failed(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when manager planning returned a non-zero exit."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status

    blocker = dict(ctx.status.get("blocker") or {})
    msg = str(
        blocker.get("message")
        or ctx.status.get("phase", {}).get("detail")
        or "Manager planning failed for task."
    ).strip()
    kind = str(blocker.get("kind") or "manager").strip() or "manager"
    phase_detail = str(ctx.status.get("phase", {}).get("detail") or msg).strip()
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="planning",
        phase_detail=phase_detail,
        run_status="blocked",
        blocker=(True, kind, msg, "manager"),
        run_completed=True,
    )
    _touch_coordination(
        ctx.coordination,
        run_id=ctx.run_id,
        lease_id=ctx.lease_id,
        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
        status="blocked",
        phase="planning",
        error=msg,
        completed=True,
    )
    save_run_text(ctx.layout["summary"], build_blocked_summary(task_title=ctx.task["title"], message=msg))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=msg, coordination=ctx.coordination)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": kind, "detail": phase_detail})
    return ctx.make_result()


def _complete_pre_satisfied(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a completed-run result when all subtasks were pre-satisfied."""
    from src.tandem_agents.core.engine.engine import git_diff_stat
    from src.tandem_agents.runtime.run_output import (
        build_completed_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    _append_blackboard_note(ctx.blackboard, "Repository already satisfied the expected files; skipping worker execution.")
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    diff = git_diff_stat(ctx.repo_path)
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail="repository already satisfied task",
        run_status="completed", run_completed=True,
        metrics={**_worker_result_metrics(ctx.worker_results),
                 "planned_workers": len(ctx.planned_subtasks), "tests_passed": True},
    )
    provider_meta = ctx.status.get("provider") if isinstance(ctx.status.get("provider"), dict) else {}
    save_run_text(ctx.layout["summary"], build_completed_summary(
        run_id=ctx.run_id, task_title=ctx.task["title"], repo_path=ctx.repo.get("path"),
        engine_label=ctx.engine.get("version") or ctx.engine.get("status") or "unknown",
        provider_id=str(provider_meta.get("id") or ctx.cfg.provider.id),
        provider_model=str(provider_meta.get("model") or ctx.cfg.provider.model),
        worker_results=ctx.worker_results, review_returncode=0, test_returncode=0, diff_snapshot=diff,
    ))
    sync_failed = _finalize_github_sync(
        cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
        status=ctx.status, blackboard=ctx.blackboard,
        outcome="completed", summary="Repository already satisfied the requested task.",
        diff_snapshot=diff, review_returncode=0, test_returncode=0,
        coordination=ctx.coordination,
    )
    if sync_failed:
        return block_run(
            run_id=ctx.run_id, run_dir=ctx.run_dir, layout=ctx.layout, cfg=ctx.cfg,
            task=ctx.task, repo=ctx.repo, engine=ctx.engine,
            phase="handoff",
            kind="github_sync_failed",
            message=(
                "Run was successful locally but the GitHub finalize sync hit a terminal "
                "outbox failure. The remote board will not show the completed status; "
                "investigate with `aca lease list` and the GitHub MCP logs."
            ),
            phase_detail="github finalize outbox dispatch hit terminal failure",
            coordination=ctx.coordination,
            existing_status=ctx.status,
        )
    task_key, lease_id, worker_id, host_id, _ = ctx.coordination_task_context()
    if task_key and lease_id and worker_id and host_id:
        ctx.coordination.mark_task_done(task_key, run_id=ctx.run_id, lease_id=lease_id,
                                        worker_id=worker_id, host_id=host_id,
                                        reason="repository already satisfied task")
    append_event(ctx.layout["events"], "run.completed", ctx.run_id, {"kind": "verified_existing"})
    return ctx.make_result()


def _block_worker_failure(ctx: "_PhaseRunContext") -> dict[str, Any]:
    """Return a blocked-run result when one or more workers failed critically."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    blocker = _worker_failure_blocker(ctx.worker_results)
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="worker_execution",
        phase_detail=blocker["phase_detail"], run_status="blocked",
        blocker=(True, blocker["kind"], blocker["message"], "worker"),
        run_completed=True,
    )
    ctx.status.setdefault("blocker", {})["detail"] = blocker["detail"]
    ctx.status.setdefault("blocker", {})["recovery_action"] = blocker["recovery_action"]
    if blocker.get("engine"):
        ctx.status.setdefault("engine", {}).update(blocker["engine"])
    ctx.status.setdefault("artifacts", {})
    for key in ("events_path", "messages_path", "sync_snapshot_path"):
        value = (blocker.get("engine") or {}).get(key)
        if value:
            ctx.status["artifacts"][f"engine_{key.replace('_path', '')}"] = value
    ctx.blackboard.setdefault("blockers", []).append(
        {
            "kind": blocker["kind"],
            "message": blocker["message"],
            "detail": blocker["detail"],
            "recovery_action": blocker["recovery_action"],
            "phase": "worker_execution",
        }
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="worker_execution",
                        error=blocker["message"], completed=True)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", blocker["kind"])
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"],
        message="\n".join(
            line
            for line in (
                blocker["message"],
                f"Detail: {blocker['detail']}" if blocker.get("detail") else "",
                f"Recovery: {blocker['recovery_action']}" if blocker.get("recovery_action") else "",
            )
            if line
        ),
        worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=blocker["message"],
                          coordination=ctx.coordination)
    append_event(
        ctx.layout["events"],
        "run.blocked",
        ctx.run_id,
        {
            "kind": blocker["kind"],
            "detail": blocker["detail"],
            "recovery_action": blocker["recovery_action"],
        },
    )
    return ctx.make_result()


def _block_integration_failed(ctx: "_PhaseRunContext", message: str | None = None) -> dict[str, Any]:
    """Return a blocked-run result when the integration prompt failed."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    blocker_message = message or "Integration prompt failed after worker completion."
    _preserve_and_reset_blocked_worktree(ctx, reason="integration_failed")
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail=blocker_message, run_status="blocked",
        blocker=(True, "integration", blocker_message, "manager"),
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="handoff", error=blocker_message)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", "integration failure")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"], message=blocker_message,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=blocker_message,
                          review_returncode=None, test_returncode=None,
                          coordination=ctx.coordination)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": "integration", "detail": blocker_message})
    return ctx.make_result()


def _block_verification_failed(ctx: "_PhaseRunContext", verification: Any) -> dict[str, Any]:
    """Return a blocked-run result when verification failed after all retries."""
    from src.tandem_agents.runtime.run_output import (
        build_blocked_summary, save_run_text, set_status, write_board_snapshot, write_blackboard_snapshot
    )
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.runstate import save_blackboard

    failure_category = str(getattr(verification, "failure_category", None) or verification.outcome).strip() or verification.outcome
    label = failure_category.replace("_", "-")
    blocker_msg = verification.validation_blocker or "Review or test failed"
    _preserve_and_reset_blocked_worktree(ctx, reason=f"verification_{failure_category}")
    ctx.status = set_status(
        ctx.status, ctx.layout, phase="handoff",
        phase_detail=f"{label}: {blocker_msg}",
        run_status="blocked",
        blocker=(True, failure_category, blocker_msg, "reviewer"),
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase="handoff", error=blocker_msg)
    _move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", f"{label} validation failure")
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"],
        message=f"{label}: {blocker_msg}",
        worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=f"{label}: {blocker_msg}",
                          diff_snapshot=ctx.pending_diff_snapshot,
                          review_returncode=ctx.review_result.get("returncode"),
                          test_returncode=ctx.test_result.get("returncode"),
                          coordination=ctx.coordination)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": failure_category})
    return ctx.make_result()


def _block_from_decision(ctx: "_PhaseRunContext", decision: "RepairDecision") -> dict[str, Any]:
    """Convert a RepairDecision(action='block') into a blocked-run result dict."""
    from src.tandem_agents.runtime.run_output import build_blocked_summary, save_run_text, set_status, write_blackboard_snapshot
    from src.tandem_agents.runtime.runstate import save_blackboard

    msg = decision.message or "Run blocked."
    ctx.status = set_status(
        ctx.status, ctx.layout, phase=decision.phase,
        phase_detail=msg, run_status="blocked",
        blocker=(True, decision.kind or "unknown", msg, "manager"),
        run_completed=True,
    )
    _touch_coordination(ctx.coordination, run_id=ctx.run_id, lease_id=ctx.lease_id,
                        lease_ttl_seconds=ctx.cfg.coordination.lease_ttl_seconds,
                        status="blocked", phase=decision.phase, error=msg, completed=True)
    save_run_text(ctx.layout["summary"], build_blocked_summary(
        task_title=ctx.task["title"], message=msg, worker_results=ctx.worker_results,
    ))
    _finalize_github_sync(cfg=ctx.cfg, task=ctx.task, run_id=ctx.run_id, layout=ctx.layout,
                          status=ctx.status, blackboard=ctx.blackboard,
                          outcome="blocked", summary=msg,
                          review_returncode=None, test_returncode=None,
                          coordination=ctx.coordination)
    append_event(ctx.layout["events"], "run.blocked", ctx.run_id, {"kind": decision.kind or "unknown"})
    return ctx.make_result()
