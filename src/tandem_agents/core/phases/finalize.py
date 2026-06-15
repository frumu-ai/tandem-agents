"""phases/finalize.py -- Commit, push, PR creation, and final run status.

This module owns the happy-path completion steps after verification passes:
1. Commit the validated repository changes
2. Push the branch to the remote
3. Enqueue the GitHub PR creation outbox event and dispatch it
4. Write the final completed summary
5. Finalize GitHub project sync
6. Mark coordination as completed
7. Release the lease

All of these steps are pure I/O against the output directory, git, and
coordination store — no LLM calls happen here.
"""
from __future__ import annotations

import logging
from typing import Any

from src.tandem_agents.core.phases.context import RunContext

logger = logging.getLogger("aca.phases.finalize")


def finalize_completed_run(ctx: RunContext) -> dict[str, Any]:
    """Complete the run: commit, push, PR, summary, and coordination cleanup.

    Mutates ctx.status, ctx.blackboard in-place.

    Returns:
        Standard run-result dict suitable for returning from _run_once_internal.
    """
    from src.tandem_agents.core.engine.engine import (
        commit_repository_changes,
        current_repository_branch,
        git_diff_stat,
        push_repository_changes_result,
    )
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.runtime.runstate import append_event, save_blackboard
    from src.tandem_agents.runtime.run_output import (
        build_completed_summary,
        save_run_text,
        set_status,
        write_blackboard_snapshot,
        write_diff_snapshot,
    )

    # Step 1: Commit, but only from the run branch. If anything has left the
    # checkout on main/default, block before polluting the shared checkout.
    current_branch = current_repository_branch(ctx.repo_path, cfg=ctx.cfg)
    if current_branch != ctx.branch_name:
        return _block_finalization_failure(
            ctx,
            kind="run_branch_mismatch",
            message=(
                f"ACA expected to finalize on branch `{ctx.branch_name}` but the "
                f"repository is on `{current_branch or 'unknown'}`. No commit was "
                "created; reset the checkout to the default branch and rerun."
            ),
        )

    commit_info = commit_repository_changes(
        ctx.cfg, ctx.repo_path, f"aca: {ctx.task['title']}"
    )
    final_diff_snapshot = git_diff_stat(ctx.repo_path)
    write_diff_snapshot(ctx.layout["diffs"], ctx.pending_diff_snapshot, final_diff_snapshot)
    logger.info(
        "Committed changes: %s (run_id=%s)",
        (commit_info or {}).get("commit", "no commit"),
        ctx.run_id,
    )

    # Step 2: Update blackboard
    ctx.blackboard["artifacts"].extend([
        str(ctx.layout["summary"]),
        str(ctx.layout["status"]),
        str(ctx.layout["events"]),
    ])
    ctx.blackboard["workers"] = ctx.worker_results
    ctx.blackboard["review"] = ctx.review_result
    ctx.blackboard["test"] = ctx.test_result

    # Step 3: Push + PR creation. A code-edit run is not complete until the
    # branch is pushed and the PR creation outbox produces a PR URL.
    if commit_info:
        ctx.blackboard["commit"] = commit_info
        _rc._append_blackboard_note(
            ctx.blackboard,
            f"Committed validated changes as `{commit_info['commit'][:7]}`.",
        )
        push_result = push_repository_changes_result(ctx.cfg, ctx.repo_path, ctx.branch_name)
        if push_result.returncode == 0:
            _rc._append_blackboard_note(
                ctx.blackboard,
                f"Pushed branch `{ctx.branch_name}` to remote.",
            )
            if not _enqueue_and_dispatch_pr(ctx, final_diff_snapshot):
                _rc._append_blackboard_note(
                    ctx.blackboard,
                    "Blocked: GitHub PR creation did not return a pull request URL.",
                )
                save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
                write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
                return _block_finalization_failure(
                    ctx,
                    kind="pull_request_missing",
                    message=(
                        "ACA committed and pushed the branch, but GitHub PR creation "
                        "did not return a pull request URL. The task cannot be marked "
                        "complete without a reviewable PR."
                    ),
                )
        else:
            push_detail = (push_result.stderr or push_result.stdout or "git push failed").strip()
            _rc._append_blackboard_note(
                ctx.blackboard,
                f"Warning: Failed to push branch `{ctx.branch_name}`: {push_detail}",
            )
            logger.warning(
                "Failed to push branch %s (run_id=%s): %s",
                ctx.branch_name,
                ctx.run_id,
                push_detail,
            )
            save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
            write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)
            return _block_finalization_failure(
                ctx,
                kind="push_failed",
                message=(
                    f"ACA committed local changes as `{commit_info['commit'][:7]}`, "
                    f"but failed to push branch `{ctx.branch_name}`: {push_detail}. "
                    "No pull request was created, so the task remains blocked."
                ),
            )

    save_blackboard(ctx.layout["blackboard"], ctx.blackboard)
    write_blackboard_snapshot(ctx.run_dir, ctx.blackboard)

    # Step 4: Final completed status
    ctx.status = set_status(
        ctx.status,
        ctx.layout,
        phase="handoff",
        phase_detail="task completed",
        run_status="completed",
        run_completed=True,
        metrics={
            "planned_workers": len(ctx.planned_subtasks),
            "completed_workers": ctx.status["metrics"]["completed_workers"],
            "failed_workers": ctx.status["metrics"]["failed_workers"],
            "tests_passed": (
                ctx.review_result.get("returncode") == 0
                and ctx.test_result.get("returncode") == 0
            ),
        },
    )
    _rc._touch_coordination(
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
            reason="task completed",
        )

    _rc._move_task_card_if_present(ctx.board, ctx.task, "review", "manager", "run completed; awaiting review")
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.run_output import write_board_snapshot
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)

    # Step 5: Final summary
    provider_meta = ctx.status.get("provider") if isinstance(ctx.status.get("provider"), dict) else {}
    save_run_text(
        ctx.layout["summary"],
        build_completed_summary(
            run_id=ctx.run_id,
            task_title=ctx.task["title"],
            repo_path=ctx.repo.get("path"),
            engine_label=(
                ctx.status.get("engine", {}).get("version")
                or ctx.status.get("engine", {}).get("build_id")
                or "unknown"
            ),
            provider_id=str(provider_meta.get("id") or ctx.cfg.provider.id),
            provider_model=str(provider_meta.get("model") or ctx.cfg.provider.model),
            worker_results=ctx.worker_results,
            review_returncode=ctx.review_result.get("returncode"),
            test_returncode=ctx.test_result.get("returncode"),
            diff_snapshot=final_diff_snapshot,
        ),
    )
    sync_failed = _rc._finalize_github_sync(
        cfg=ctx.cfg,
        task=ctx.task,
        run_id=ctx.run_id,
        layout=ctx.layout,
        status=ctx.status,
        blackboard=ctx.blackboard,
        outcome="completed",
        summary="Run completed successfully.",
        diff_snapshot=final_diff_snapshot,
        review_returncode=ctx.review_result.get("returncode"),
        test_returncode=ctx.test_result.get("returncode"),
    )
    if sync_failed:
        # Local work succeeded but the GitHub board did not advance — block
        # the run so the operator sees the divergence rather than a green
        # success that contradicts the remote state.
        from src.tandem_agents.core.execution.run_lifecycle import block_run

        return block_run(
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            layout=ctx.layout,
            cfg=ctx.cfg,
            task=ctx.task,
            repo=ctx.repo,
            engine=ctx.engine,
            phase="handoff",
            kind="github_sync_failed",
            message=(
                "Run completed locally but the GitHub finalize sync hit a terminal "
                "outbox failure. The remote board will not show the completed status; "
                "investigate the GitHub MCP logs and the coordination outbox before "
                "re-running."
            ),
            phase_detail="github finalize outbox dispatch hit terminal failure",
            coordination=ctx.coordination,
            existing_status=ctx.status,
        )

    append_event(
        ctx.layout["events"],
        "run.completed",
        ctx.run_id,
        {"result": "completed"},
        task_id=ctx.task.get("task_id"),
        role="manager",
        repo={"path": ctx.repo.get("path")},
    )

    logger.info("Run completed successfully (run_id=%s)", ctx.run_id)
    return ctx.make_result(worker_results=ctx.worker_results, board=ctx.board)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enqueue_and_dispatch_pr(ctx: RunContext, final_diff_snapshot: str) -> bool:
    """Enqueue a github_pull_request.create outbox event and dispatch it now."""
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.core.phases.pr_body import build_pull_request_body
    from src.tandem_agents.runtime.runstate import append_event, write_status

    pr_body = build_pull_request_body(ctx, final_diff_snapshot)
    ctx.coordination.enqueue_outbox(
        kind="github_pull_request.create",
        aggregate_type="task",
        aggregate_id=str(ctx.task.get("task_id") or ctx.run_id),
        payload={
            "run_id": ctx.run_id,
            "task": ctx.task,
            "head_branch": ctx.branch_name,
            "title": f"aca: {ctx.task['title']}",
            "body": pr_body,
        },
        dedupe_key=f"{ctx.run_id}:github:create-pr",
    )
    pr_summary = _rc._dispatch_outbox_now(ctx.cfg, ctx.coordination, limit=25)
    for result in pr_summary.get("items") or []:
        payload = dict(result.get("payload") or {})
        if str(payload.get("run_id") or "") != ctx.run_id:
            continue
        if str(result.get("kind") or "") != "github_pull_request.create":
            continue
        if str(result.get("status") or "").strip().lower() != "dispatched":
            continue
        pr_url = str(result.get("pr_url") or "").strip()
        if pr_url:
            pull_request = dict(result.get("pull_request") or {})
            if not pull_request:
                pull_request = {
                    "url": pr_url,
                    "head_branch": ctx.branch_name,
                    "base_branch": ctx.cfg.repository.default_branch or "main",
                    "base_repo": ctx.cfg.repository.slug,
                    "lifecycle_state": "waiting-for-review",
                    "terminal": False,
                }
            ctx.blackboard["pull_request"] = pr_url
            ctx.blackboard["pull_request_lifecycle"] = pull_request
            ctx.status["pull_request"] = pr_url
            ctx.status["pull_request_lifecycle"] = pull_request
            ctx.status.setdefault("task", {})["pull_request"] = pr_url
            ctx.status["task"]["pull_request_lifecycle"] = pull_request
            try:
                run = ctx.coordination.get_run(ctx.run_id) or {}
                metadata = dict(run.get("metadata") or {})
                metadata["pull_request"] = pr_url
                metadata["pull_request_lifecycle"] = pull_request
                ctx.coordination.update_run(ctx.run_id, metadata=metadata)
            except Exception:
                logger.debug("Failed to persist PR lifecycle metadata for run %s", ctx.run_id, exc_info=True)
            write_status(ctx.layout["status"], ctx.status)
            _rc._append_blackboard_note(ctx.blackboard, f"Created Pull Request: {pr_url}")
            append_event(
                ctx.layout["events"],
                "github_pull_request.created",
                ctx.run_id,
                {"url": pr_url, "pull_request": pull_request},
            )
            logger.info("Pull request created: %s (run_id=%s)", pr_url, ctx.run_id)
            return True
    return False


def _block_finalization_failure(
    ctx: RunContext,
    *,
    kind: str,
    message: str,
) -> dict[str, Any]:
    from src.tandem_agents.core.execution import runner_core as _rc
    from src.tandem_agents.core.execution.run_lifecycle import block_run
    from src.tandem_agents.core.repository.board import save_board
    from src.tandem_agents.runtime.run_output import write_board_snapshot

    _rc._move_task_card_if_present(ctx.board, ctx.task, "blocked", "manager", kind)
    save_board(ctx.board_path, ctx.board)
    write_board_snapshot(ctx.run_dir, ctx.board)
    return block_run(
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        layout=ctx.layout,
        cfg=ctx.cfg,
        task=ctx.task,
        repo=ctx.repo,
        engine=ctx.engine,
        phase="handoff",
        kind=kind,
        message=message,
        phase_detail=message,
        coordination=ctx.coordination,
        worker_results=ctx.worker_results,
        existing_status=ctx.status,
    )
