from __future__ import annotations

import json
import time
import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.execution.worker import (
    _annotate_ignored_target_files,
    _apply_carry_forward_patch,
    _async_no_text_timeout_seconds,
    _async_prompt_timeout_seconds,
    _call_with_timeout,
    _clear_active_worker_attempt,
    _coerce_worker_failure,
    _diff_touches_nearby_test_files,
    _engine_max_events_without_text,
    _empty_transcript_retry_prompt,
    _extract_partial_worker_diff_artifact,
    _extract_prompt_sync_text,
    _extract_session_reply,
    _git_ignored_paths,
    _manager_plan_stream_complete,
    _materialize_worker_context,
    _preserve_partial_worker_diff,
    _recover_nonzero_result_with_diff,
    _recover_nonzero_result_if_diff_satisfies_subtask,
    _recover_seeded_pr_candidate_diff,
    _seed_pr_candidate_diff,
    _seedable_pr_candidate_specs,
    _scaled_async_no_text_timeout_seconds,
    _scaled_async_prompt_timeout_seconds,
    _scaled_prompt_sync_timeout_seconds,
    _substantive_target_files,
    _subtask_requires_real_diff,
    _support_only_changed_files_for_subtask,
    _terminalized_note_reports_blockers,
    _terminalize_worker_after_tool_loop,
    _worktree_changed_files,
    _worker_result_should_retry,
    _worker_execution_worktree_name,
    _worker_note_reports_blocked,
    _worker_timeout_multiplier,
    _worker_prompt_retry_suffix,
    run_worker_subtask,
    stream_tandem_prompt,
    summarize_worker_notes,
)
from src.tandem_agents.core.phases.worker_dispatch import (
    _apply_tolerated_failures,
    _changed_files_satisfy_required_test_files,
    _changed_python_syntax_errors,
    _clear_active_worker_attempt_marker,
    _diff_has_unproductive_marker,
    _diff_has_tautological_boolean_assertion,
    _diff_has_placeholder_noop_test,
    _diff_applies_to_head,
    _diff_is_comment_only,
    _diff_is_local_string_oracle_test,
    _diff_is_string_only_change,
    _diff_changed_files_missing_substantive_production_followup,
    _diff_missing_production_function_calls,
    _subtask_has_required_test_only_diff,
    _subtask_has_verifiable_source_and_test_diff,
    _sync_verifiable_worker_diff,
    _subtask_requires_test_changes,
    _subtask_required_test_files,
    _worker_no_progress_timeout_seconds,
    _worker_comment_only_diff_abort_seconds,
    _worker_testless_diff_abort_seconds,
    _worker_verifiable_diff_abort_seconds,
)


class WorkerFailureCoercionTest(unittest.TestCase):
    def test_dash_blocked_heading_reports_blocker(self) -> None:
        self.assertTrue(
            _worker_note_reports_blocked(
                "Blocked \u2014 I inspected the patch but could not complete fixes before the tool session ended.\n"
                "\nBlocker:\n- Verification failed.\n"
            )
        )
        self.assertFalse(_worker_note_reports_blocked("Blocked by dependency analysis is not a final blocked status."))

    def test_worker_clear_active_attempt_removes_matching_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            marker = run_dir / "active_worker_attempts.json"
            marker.write_text(json.dumps({"worker-1": "exec-1", "worker-2": "exec-2"}), encoding="utf-8")
            layout = {"run_dir": run_dir}

            _clear_active_worker_attempt(layout, "worker-1", "exec-1")

            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"worker-2": "exec-2"})

    def test_worker_clear_active_attempt_keeps_newer_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            marker = run_dir / "active_worker_attempts.json"
            marker.write_text(json.dumps({"worker-1": "new-exec"}), encoding="utf-8")
            layout = {"run_dir": run_dir}

            _clear_active_worker_attempt(layout, "worker-1", "old-exec")

            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"worker-1": "new-exec"})

    def test_dispatch_abort_clears_active_worker_attempt_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            marker = run_dir / "active_worker_attempts.json"
            marker.write_text(
                json.dumps({"worker-1": "old-exec", "worker-2": "other-exec"}),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(run_dir=run_dir)

            _clear_active_worker_attempt_marker(ctx, "worker-1")

            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")),
                {"worker-2": "other-exec"},
            )

            _clear_active_worker_attempt_marker(ctx, "worker-2")

            self.assertFalse(marker.exists())

    def test_worker_execution_worktree_name_uses_safe_internal_override(self) -> None:
        self.assertEqual(
            _worker_execution_worktree_name(
                "worker-1",
                {
                    "id": "subtask-1",
                    "_worker_worktree_name": "worker-1--subtask-1--exec-123",
                },
            ),
            "worker-1--subtask-1--exec-123",
        )
        self.assertEqual(
            _worker_execution_worktree_name(
                "worker-1",
                {
                    "id": "subtask-1",
                    "_worker_worktree_name": "../shared-repo",
                },
            ),
            "worker-1--subtask-1",
        )

    def test_worker_no_progress_timeout_derives_from_effective_worker_budget(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "120",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "240",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "20",
                }
            )
        )

        timeout = _worker_no_progress_timeout_seconds(
            ctx,
            [{"id": "subtask-1", "title": "Small task", "files": ["src/app.py"]}],
        )

        self.assertEqual(timeout, 290.0)

    def test_worker_no_progress_timeout_scales_for_large_subtasks(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(
                env={
                    "ACA_WORKER_PROMPT_SYNC_TIMEOUT_SECONDS": "120",
                    "ACA_WORKER_PROMPT_SYNC_MAX_TIMEOUT_SECONDS": "240",
                    "ACA_WORKER_TERMINALIZE_TIMEOUT_SECONDS": "20",
                }
            )
        )

        timeout = _worker_no_progress_timeout_seconds(
            ctx,
            [
                {
                    "id": "subtask-1",
                    "title": "Large task",
                    "files": [
                        "crates/a/src/lib.rs",
                        "crates/a/src/http.rs",
                        "crates/a/src/model.rs",
                        "crates/a/src/store.rs",
                        "crates/a/src/test.rs",
                    ],
                }
            ],
        )

        self.assertGreater(timeout, 290.0)

    def test_worker_no_progress_timeout_env_override_still_wins(self) -> None:
        ctx = SimpleNamespace(
            cfg=SimpleNamespace(env={"ACA_WORKER_NO_PROGRESS_TIMEOUT_SECONDS": "42"})
        )

        self.assertEqual(_worker_no_progress_timeout_seconds(ctx, []), 42.0)

    def test_testless_diff_guard_detects_regression_subtasks_with_test_targets(self) -> None:
        subtask = {
            "title": "Add schema drift regression coverage",
            "goal": "Cover degraded read readiness",
            "files": [
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
            "acceptance_criteria": ["Adds regression tests for schema drift."],
        }

        self.assertEqual(
            _subtask_required_test_files(subtask),
            ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
        )
        self.assertTrue(_subtask_requires_test_changes(subtask))

    def test_testless_diff_guard_requires_declared_test_target_when_present(self) -> None:
        required = ["src/tandem_agents/api/run_isolation_test.py"]

        self.assertFalse(
            _changed_files_satisfy_required_test_files(
                ["src/tandem_agents/api/run_isolation.py", "tests/api/test_run_isolation.py"],
                required,
            )
        )
        self.assertTrue(
            _changed_files_satisfy_required_test_files(
                ["src/tandem_agents/api/run_isolation.py", "src/tandem_agents/api/run_isolation_test.py"],
                required,
            )
        )

    def test_testless_diff_guard_ignores_plain_implementation_subtasks(self) -> None:
        subtask = {
            "title": "Refine readiness payload",
            "goal": "Update production response",
            "files": [
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
            "acceptance_criteria": ["Readiness fields are clear."],
        }

        self.assertFalse(_subtask_requires_test_changes(subtask))

    def test_testless_diff_guard_timeout_env_override_can_disable(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_TESTLESS_DIFF_ABORT_SECONDS": "0"}))

        self.assertEqual(_worker_testless_diff_abort_seconds(ctx), 0.0)

    def test_comment_only_diff_guard_timeout_env_override_can_disable(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_COMMENT_ONLY_DIFF_ABORT_SECONDS": "0"}))

        self.assertEqual(_worker_comment_only_diff_abort_seconds(ctx), 0.0)

    def test_verifiable_diff_guard_timeout_env_override_can_disable(self) -> None:
        ctx = SimpleNamespace(cfg=SimpleNamespace(env={"ACA_WORKER_VERIFIABLE_DIFF_ABORT_SECONDS": "0"}))

        self.assertEqual(_worker_verifiable_diff_abort_seconds(ctx), 0.0)

    def test_changed_python_syntax_errors_reports_invalid_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.py"
            invalid = root / "invalid.py"
            valid.write_text("value = 1\n", encoding="utf-8")
            invalid.write_text("def broken(:\n    pass\n", encoding="utf-8")

            self.assertEqual(_changed_python_syntax_errors(root, ["valid.py"]), [])
            errors = _changed_python_syntax_errors(root, ["valid.py", "invalid.py", "README.md"])

            self.assertEqual(len(errors), 1)
            self.assertIn("invalid.py", errors[0])
            self.assertIn("invalid syntax", errors[0])

    def test_sync_verifiable_worker_diff_emits_synced_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / "events.jsonl"
            ctx = SimpleNamespace(
                repo_path=root / "repo",
                layout={"events": events},
                run_id="run-1",
                task={"task_id": "TAN-170"},
                repo={"path": str(root / "repo")},
            )
            worktree = root / "worktree"
            with mock.patch(
                "src.tandem_agents.core.phases.worker_dispatch.sync_worktree_changes",
                return_value=["src/app.py", "src/app_test.py"],
            ):
                ok, changed, synced, error = _sync_verifiable_worker_diff(
                    ctx,
                    worker_id="worker-1",
                    subtask_id="subtask-1",
                    worktree=worktree,
                    changed_files=["src/app.py"],
                )

            self.assertTrue(ok)
            self.assertEqual(changed, ["src/app.py", "src/app_test.py"])
            self.assertEqual(synced, ["src/app.py", "src/app_test.py"])
            self.assertEqual(error, "")
            event = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(event["type"], "worker.verifiable_diff_synced")
            self.assertEqual(event["payload"]["changed_files"], ["src/app.py", "src/app_test.py"])

    def test_sync_verifiable_worker_diff_reports_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = SimpleNamespace(
                repo_path=root / "repo",
                layout={"events": root / "events.jsonl"},
                run_id="run-1",
                task={},
                repo={},
            )
            with mock.patch(
                "src.tandem_agents.core.phases.worker_dispatch.sync_worktree_changes",
                side_effect=RuntimeError("copy failed"),
            ):
                ok, changed, synced, error = _sync_verifiable_worker_diff(
                    ctx,
                    worker_id="worker-1",
                    subtask_id="subtask-1",
                    worktree=root / "worktree",
                    changed_files=["src/app.py", "src/app_test.py"],
                )

            self.assertFalse(ok)
            self.assertEqual(changed, [])
            self.assertEqual(synced, [])
            self.assertIn("copy failed", error)

    def test_required_test_only_diff_detects_incomplete_regression_attempt(self) -> None:
        subtask = {
            "title": "Finish repository isolation regression tests",
            "goal": "Add regression coverage",
            "files": [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            "acceptance_criteria": ["Run repository regression tests."],
        }

        self.assertTrue(
            _subtask_has_required_test_only_diff(
                subtask,
                ["src/tandem_agents/core/repository/repository_test.py"],
            )
        )
        self.assertFalse(
            _subtask_has_required_test_only_diff(
                subtask,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )
        )
        self.assertFalse(
            _subtask_has_required_test_only_diff(
                subtask,
                ["tests/repository_test.py"],
            )
        )
        plain_subtask = {
            **subtask,
            "title": "Update repository helper behavior",
            "goal": "Refine branch naming behavior",
            "acceptance_criteria": ["Repository helper behavior is updated."],
        }
        self.assertFalse(
            _subtask_has_required_test_only_diff(
                plain_subtask,
                ["src/tandem_agents/core/repository/repository_test.py"],
            )
        )

    def test_verifiable_diff_guard_requires_source_and_declared_test_target(self) -> None:
        subtask = {
            "title": "Finish repository isolation regression tests",
            "goal": "Add regression coverage",
            "files": [
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            "acceptance_criteria": ["Run repository regression tests."],
        }

        self.assertTrue(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
            )
        )
        self.assertFalse(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                ["src/tandem_agents/core/repository/repository.py"],
            )
        )
        self.assertFalse(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                ["src/tandem_agents/core/repository/repository_test.py"],
            )
        )
        self.assertFalse(
            _subtask_has_verifiable_source_and_test_diff(
                subtask,
                [
                    "src/tandem_agents/core/repository/repository.py",
                    "tests/repository_test.py",
                ],
            )
        )

    def test_unproductive_diff_marker_detects_placeholder_blocker_test(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -1,3 +1,8 @@
+#[test]
+fn github_projects_regression_coverage_requires_production_path() {
+    // TODO(worker-blocker): replace with production-path regression coverage before merging.
+    panic!("blocked: production-path regression coverage was not added or verified");
+}
 """

        self.assertTrue(_diff_has_unproductive_marker(diff))
        self.assertFalse(_diff_is_comment_only(diff))

    def test_comment_only_diff_detects_only_added_comments(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -1,3 +1,4 @@
+// CRI-02 coverage belongs in this module.
 #[tokio::test]
 async fn existing_test() {}
 """

        self.assertTrue(_diff_is_comment_only(diff))
        self.assertFalse(_diff_has_unproductive_marker(diff))

    def test_test_only_repair_rejects_comment_only_production_followup(self) -> None:
        diff = """diff --git a/src/repository.py b/src/repository.py
--- a/src/repository.py
+++ b/src/repository.py
@@ -1,3 +1,5 @@
 from __future__ import annotations
+# Keep run isolation helpers together for future callers.
+
 import os
diff --git a/src/repository_test.py b/src/repository_test.py
--- a/src/repository_test.py
+++ b/src/repository_test.py
@@ -1,2 +1,6 @@
+def test_worker_name_includes_run_id():
+    assert worker_worktree_name("worker-1", "subtask-1")
 """

        self.assertEqual(
            _diff_changed_files_missing_substantive_production_followup(
                diff,
                ["src/repository.py", "src/repository_test.py"],
                {"repair_requires_production_followup": ["src/repository.py"]},
            ),
            ["src/repository.py"],
        )

    def test_test_only_repair_accepts_substantive_production_followup(self) -> None:
        diff = """diff --git a/src/repository.py b/src/repository.py
--- a/src/repository.py
+++ b/src/repository.py
@@ -1,3 +1,5 @@
 from __future__ import annotations
+MAX_WORKTREE_NAME_LENGTH = 96
+
 import os
diff --git a/src/repository_test.py b/src/repository_test.py
--- a/src/repository_test.py
+++ b/src/repository_test.py
@@ -1,2 +1,6 @@
+def test_worker_name_includes_run_id():
+    assert worker_worktree_name("worker-1", "subtask-1")
 """

        self.assertEqual(
            _diff_changed_files_missing_substantive_production_followup(
                diff,
                ["src/repository.py", "src/repository_test.py"],
                {"repair_requires_production_followup": ["src/repository.py"]},
            ),
            [],
        )

    def test_tautological_boolean_assertion_diff_is_unproductive(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -2,6 +2,8 @@
 async fn coder_memory_events_include_normalized_artifact_fields() {
     let state = test_state().await;
+    let schema_drift_readiness_regression_exercises_part09 = true;
+    assert!(schema_drift_readiness_regression_exercises_part09);
     state
 """

        self.assertTrue(_diff_has_tautological_boolean_assertion(diff))
        self.assertFalse(_diff_is_string_only_change(diff))

    def test_placeholder_noop_test_diff_is_unproductive(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -1,3 +1,11 @@
+#[test]
+fn github_projects_readiness_regression_placeholder_exercises_existing_fixture() {
+    // Tooling for this continuation only exposed edit/apply/write and did not
+    // expose read/grep/bash, so this placeholder must be replaced before completion.
+    assert!(true);
+}
 """

        self.assertTrue(_diff_has_placeholder_noop_test(diff))
        self.assertFalse(_diff_is_local_string_oracle_test(diff))

    def test_string_only_test_wording_diff_is_unproductive(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -46,7 +46,7 @@ async fn coder_memory_events_include_normalized_artifact_fields() {
         .body(Body::from(
             json!({
-                "summary": "Capability readiness drift already explained this failure",
+                "summary": "Capability readiness drift already explained degraded read readiness for this failure",
                 "confidence": "high"
             })
 """

        self.assertTrue(_diff_is_string_only_change(diff))
        self.assertFalse(_diff_has_tautological_boolean_assertion(diff))

    def test_local_string_oracle_test_diff_is_unproductive(self) -> None:
        diff = """diff --git a/tests/part09.rs b/tests/part09.rs
--- a/tests/part09.rs
+++ b/tests/part09.rs
@@ -1,3 +1,19 @@
+#[test]
+fn github_projects_readiness_regression_language_distinguishes_read_and_write_degradation() {
+    let read_degraded = "GitHub Projects read readiness degraded: schema drift or remote divergence";
+    let write_degraded = "GitHub Projects write readiness degraded: mutation capability unavailable";
+
+    assert!(read_degraded.contains("read readiness degraded"));
+    assert!(read_degraded.contains("schema drift"));
+    assert!(read_degraded.contains("remote divergence"));
+    assert!(write_degraded.contains("write readiness degraded"));
+    assert!(write_degraded.contains("mutation capability"));
+    assert_ne!(read_degraded, write_degraded);
+}
 """

        self.assertTrue(_diff_is_local_string_oracle_test(diff))
        self.assertFalse(_diff_has_tautological_boolean_assertion(diff))

    def test_test_only_diff_detects_missing_production_helper_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            source = repo / "crates" / "tandem-server" / "src" / "http" / "coder_parts" / "part09.rs"
            source.parent.mkdir(parents=True)
            source.write_text("pub fn existing() {}\n", encoding="utf-8")
            test_file = repo / "crates" / "tandem-server" / "src" / "http" / "tests" / "coder_parts" / "part09.rs"
            test_file.parent.mkdir(parents=True)
            test_file.write_text("#[test]\nfn existing_test() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            diff = """diff --git a/crates/tandem-server/src/http/tests/coder_parts/part09.rs b/crates/tandem-server/src/http/tests/coder_parts/part09.rs
--- a/crates/tandem-server/src/http/tests/coder_parts/part09.rs
+++ b/crates/tandem-server/src/http/tests/coder_parts/part09.rs
@@ -1,2 +1,8 @@
+#[test]
+fn github_projects_regression_exercises_production_path() {
+    let readiness = github_projects_intake_readiness_from_project_v2(&serde_json::json!({}));
+    assert!(format!("{readiness:?}").contains("readiness"));
+}
 """

            self.assertEqual(
                _diff_missing_production_function_calls(
                    repo,
                    diff,
                    ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                ),
                ["github_projects_intake_readiness_from_project_v2"],
            )

    def test_test_only_diff_allows_existing_production_helper_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            source = repo / "crates" / "tandem-server" / "src" / "http" / "coder_parts" / "part09.rs"
            source.parent.mkdir(parents=True)
            source.write_text(
                "pub fn github_projects_intake_readiness_from_project_v2() {}\n",
                encoding="utf-8",
            )
            test_file = repo / "crates" / "tandem-server" / "src" / "http" / "tests" / "coder_parts" / "part09.rs"
            test_file.parent.mkdir(parents=True)
            test_file.write_text("#[test]\nfn existing_test() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            diff = """diff --git a/crates/tandem-server/src/http/tests/coder_parts/part09.rs b/crates/tandem-server/src/http/tests/coder_parts/part09.rs
--- a/crates/tandem-server/src/http/tests/coder_parts/part09.rs
+++ b/crates/tandem-server/src/http/tests/coder_parts/part09.rs
@@ -1,2 +1,8 @@
+#[test]
+fn github_projects_regression_exercises_production_path() {
+    github_projects_intake_readiness_from_project_v2();
+}
 """

            self.assertEqual(
                _diff_missing_production_function_calls(
                    repo,
                    diff,
                    ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                ),
                [],
            )

    def test_session_reply_extracts_assistant_text_from_engine_messages(self) -> None:
        messages = [
            {"info": {"role": "user"}, "parts": [{"text": "prompt"}]},
            {"info": {"role": "assistant"}, "parts": [{"content": [{"text": "answer"}]}]},
        ]

        self.assertEqual(_extract_session_reply(messages), "answer")

    def test_prompt_sync_text_extracts_messages_payload(self) -> None:
        response = {
            "messages": [
                {"info": {"role": "assistant"}, "parts": [{"text": "sync answer"}]},
            ]
        }

        self.assertEqual(_extract_prompt_sync_text(response), "sync answer")

    def test_manager_plan_stream_complete_detects_finished_plan_json(self) -> None:
        self.assertFalse(_manager_plan_stream_complete('{"summary":"ok"'))
        self.assertFalse(_manager_plan_stream_complete('{"summary":"ok"}'))
        self.assertTrue(
            _manager_plan_stream_complete(
                'prefix {"summary":"ok","subtasks":[],"risks":[],"tests":[]} suffix'
            )
        )

    def test_progress_diff_validator_rejects_truncated_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            target = repo / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            target.write_text("pub fn value() -> i32 { 2 }\n", encoding="utf-8")
            diff = subprocess.run(
                ["git", "diff", "--binary"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            truncated = "\n".join(diff.splitlines()[:-1]) + "\n"

            self.assertTrue(_diff_applies_to_head(repo, diff))
            self.assertFalse(_diff_applies_to_head(repo, diff.strip()))
            self.assertFalse(_diff_applies_to_head(repo, truncated))

    def test_carry_forward_patch_applies_saved_partial_diff_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            target = repo / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            target.write_text("pub fn value() -> i32 { 2 }\n", encoding="utf-8")
            diff = subprocess.run(
                ["git", "diff", "--binary"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            subprocess.run(["git", "checkout", "--", "src/lib.rs"], cwd=repo, check=True)
            artifact = root / "partial.patch"
            artifact.write_text(
                "# Partial worker diff preserved after nonterminal engine result\n"
                "# Reason: ENGINE_PROMPT_TIMEOUT\n\n"
                "## git status --short --untracked-files=all\n\n M src/lib.rs\n"
                f"## git diff --binary\n\n{diff}",
                encoding="utf-8",
            )
            log_path = root / "worker.log"

            self.assertIn("diff --git", _extract_partial_worker_diff_artifact(artifact.read_text(encoding="utf-8")))
            self.assertTrue(_apply_carry_forward_patch(repo, artifact, log_path))
            self.assertEqual(target.read_text(encoding="utf-8"), "pub fn value() -> i32 { 2 }\n")
            self.assertIn("applied carry-forward", log_path.read_text(encoding="utf-8"))

    def test_run_worker_subtask_fails_closed_when_carry_forward_patch_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "run"
            worktree = root / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            repo_path.mkdir()
            patch = root / "corrupt.patch"
            patch.write_text(
                "# Partial worker diff captured during worker progress heartbeat\n"
                "## git diff --binary\n\n"
                "diff --git a/src/lib.rs b/src/lib.rs\n"
                "index 1111111..2222222 100644\n"
                "--- a/src/lib.rs\n"
                "+++ b/src/lib.rs\n"
                "@@ -1 +1 @@\n"
                "-pub fn value() -> i32 { 1 }\n",
                encoding="utf-8",
            )

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt") as stream_prompt:
                output = run_worker_subtask(
                    SimpleNamespace(),
                    "run-1",
                    repo_path,
                    run_dir,
                    {"task_id": "TAN-170", "source": {"type": "linear"}, "title": "Task"},
                    {
                        "id": "subtask-1",
                        "title": "Retry carried patch",
                        "goal": "Apply preserved patch and verify.",
                        "files": ["src/lib.rs"],
                        "carry_forward_patch": str(patch),
                    },
                    "worker-1",
                    1,
                )

            self.assertEqual(output["returncode"], 1)
            self.assertEqual(output["failure_reason"], "CARRY_FORWARD_PATCH_APPLY_FAILED")
            self.assertEqual(output["blocker_kind"], "carry_forward_patch_apply_failed")
            self.assertEqual(output["carry_forward_patch"], str(patch))
            stream_prompt.assert_not_called()
            log_text = (run_dir / "logs" / "worker-1.log").read_text(encoding="utf-8")
            self.assertIn("carry-forward patch did not apply cleanly", log_text)

    def test_preserved_partial_diff_artifact_carries_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            base = repo / "src" / "lib.rs"
            base.parent.mkdir()
            base.write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            new_test = repo / "tests" / "contract.rs"
            new_test.parent.mkdir()
            new_test.write_text("#[test]\nfn contract() { assert!(true); }\n", encoding="utf-8")
            (repo / "__aca_temp_probe.txt").write_text("placeholder\n", encoding="utf-8")
            result = _preserve_partial_worker_diff(
                {"returncode": 1, "stdout": "ENGINE_PROMPT_TIMEOUT\n"},
                log_path,
                repo,
                reason="ENGINE_PROMPT_TIMEOUT",
            )
            artifact = Path(result["partial_diff_artifact"])
            patch_text = _extract_partial_worker_diff_artifact(artifact.read_text(encoding="utf-8"))
            self.assertIn("new file mode", patch_text)
            self.assertIn("+++ b/tests/contract.rs", patch_text)
            self.assertNotIn("__aca_temp_probe.txt", patch_text)

            new_test.unlink()
            self.assertTrue(_apply_carry_forward_patch(repo, artifact, log_path))
            self.assertIn("contract", new_test.read_text(encoding="utf-8"))

    def test_aca_blocker_note_is_not_preserved_as_partial_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=repo, check=True)
            tracked = repo / "src" / "lib.rs"
            tracked.parent.mkdir()
            tracked.write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            (repo / ".aca_worker_blocker_note.txt").write_text(
                "unable to safely implement because tool access is unavailable\n",
                encoding="utf-8",
            )

            result = _preserve_partial_worker_diff(
                {"returncode": 1, "stdout": "ENGINE_PROMPT_TIMEOUT\n"},
                log_path,
                repo,
                reason="ENGINE_PROMPT_TIMEOUT",
            )

            self.assertEqual(result["changed_files"], [])
            self.assertNotIn("partial_diff_artifact", result)
            self.assertEqual(_worktree_changed_files(repo), [])

    def test_worker_summary_preserves_partial_diff_artifact_for_repair_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch_path = root / "artifacts" / "worker-1.partial-worker-diff.patch"
            patch_path.parent.mkdir()
            patch_path.write_text("diff --git a/src/lib.rs b/src/lib.rs\n", encoding="utf-8")

            result = {
                "returncode": 1,
                "stdout": "ENGINE_PROMPT_TIMEOUT",
                "log_path": str(root / "logs" / "worker-1.log"),
                "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                "blocker_kind": "worker_incomplete_diff",
                "recovery_action": "retry with partial diff",
                "partial_diff_artifact": str(patch_path),
                "artifacts": {"partial_diff": str(patch_path)},
                "changed_files": ["src/lib.rs"],
            }

            with mock.patch("src.tandem_agents.core.execution.worker.git_diff_stat", return_value=""):
                summary = summarize_worker_notes(
                    result,
                    "worker-1",
                    {"id": "subtask-1", "title": "Subtask"},
                    root / "worktree",
                    0,
                )

            self.assertEqual(summary["partial_diff_artifact"], str(patch_path))
            self.assertEqual(summary["artifacts"]["partial_diff"], str(patch_path))

    def test_blocked_worker_note_with_diff_is_preserved_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "worktrees.py"
            target.parent.mkdir()
            target.write_text("def branch_name() -> str:\n    return 'old'\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/worktrees.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("def branch_name() -> str:\n    return 'new'\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 0,
                    "stdout": (
                        "Status: blocked\n"
                        "Changed files: none by this worker.\n\n"
                        "Blocker:\n"
                        "- I cannot truthfully claim verification completed.\n"
                    ),
                    "log_path": str(log_path),
                },
                log_path,
                worktree,
                {
                    "id": "subtask-1",
                    "title": "Finish worktree helper",
                    "files": ["src/worktrees.py"],
                    "target_files": ["src/worktrees.py"],
                },
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_REPORTED_BLOCKER")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(result["changed_files"], ["src/worktrees.py"])
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())
            self.assertIn("Status: blocked", result["stdout"])
            self.assertIn("treating the worker as failed", result["stdout"])

    def test_plain_blocked_worker_note_with_diff_is_preserved_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "worktrees.py"
            target.parent.mkdir()
            target.write_text("def branch_name() -> str:\n    return 'old'\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/worktrees.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("def branch_name() -> str:\n    return 'new'\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 0,
                    "stdout": (
                        "Blocked.\n\n"
                        "Changed files:\n"
                        "- None by this worker in this turn.\n\n"
                        "Blocker:\n"
                        "- I was instructed not to call tools further in this turn.\n"
                    ),
                    "log_path": str(log_path),
                },
                log_path,
                worktree,
                {
                    "id": "subtask-1",
                    "title": "Finish worktree helper",
                    "files": ["src/worktrees.py"],
                    "target_files": ["src/worktrees.py"],
                },
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_REPORTED_BLOCKER")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(result["changed_files"], ["src/worktrees.py"])
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())
            self.assertIn("Blocked.", result["stdout"])
            self.assertIn("treating the worker as failed", result["stdout"])

    def test_malformed_blocked_status_boundary_is_preserved_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "worktrees.py"
            target.parent.mkdir()
            target.write_text("def branch_name() -> str:\n    return 'old'\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/worktrees.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("def branch_name() -> str:\n    return 'new'\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 0,
                    "stdout": (
                        "Status: blockedChanged files: none.\n\n"
                        "Validation performed:\n"
                        "- Read src/worktrees.py.\n\n"
                        "Blocker:\n"
                        "- Verification was not run.\n"
                    ),
                    "log_path": str(log_path),
                },
                log_path,
                worktree,
                {
                    "id": "subtask-1",
                    "title": "Finish worktree helper",
                    "files": ["src/worktrees.py"],
                    "target_files": ["src/worktrees.py"],
                },
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_REPORTED_BLOCKER")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(result["changed_files"], ["src/worktrees.py"])
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())
            self.assertIn("Status: blockedChanged", result["stdout"])
            self.assertIn("treating the worker as failed", result["stdout"])

    def test_terminalized_note_reports_placeholder_partial_and_unverified_work(self) -> None:
        bad_notes = [
            "Adds a placeholder test file. Verification not run.",
            "The requested suite is only partially implemented.",
            "Source inspection is still required before expanding the suite.",
            "Remaining implementation blockers: the added test would not compile due to type mismatches.",
            (
                "Remaining implementation blockers:\n"
                "- The diff does not appear to add meaningful GitHub Projects readiness drift regression coverage.\n"
                "- The added assertion is effectively a no-op/redundant test change."
            ),
        ]
        for note in bad_notes:
            with self.subTest(note=note):
                self.assertTrue(_terminalized_note_reports_blockers(note))

        self.assertFalse(
            _terminalized_note_reports_blockers(
                "Changed src/lib.rs. Verification: cargo test passed. Remaining blockers: none."
            )
        )
        self.assertFalse(
            _terminalized_note_reports_blockers(
                "Changed src/lib.rs.\nVerification: not run\nRemaining blockers:\n- None visible from the provided diff."
            )
        )
        self.assertFalse(
            _terminalized_note_reports_blockers(
                "Changed src/lib.rs.\nVerification: not run\nRemaining blockers:\n- No actual test run visible."
            )
        )
        self.assertFalse(
            _terminalized_note_reports_blockers(
                "Changed src/lib.rs.\nVerification: not run\nRemaining blockers:\n- Verification not run."
            )
        )
        self.assertFalse(
            _terminalized_note_reports_blockers(
                "Changed src/lib.rs.\nVerification:\n- verification not runRemaining implementation blockers:\n"
                "- None visible from the provided diff excerpt."
            )
        )

    def test_terminal_engine_blockers_do_not_retry_worker_prompt(self) -> None:
        self.assertTrue(
            _worker_result_should_retry({"returncode": 1, "blocker_kind": "engine_tool_loop_stalled"})
        )
        self.assertFalse(
            _worker_result_should_retry(
                {"returncode": 1, "blocker_kind": "engine_tool_loop_stalled_no_diff"}
            )
        )
        self.assertFalse(_worker_result_should_retry({"returncode": 1, "blocker_kind": "engine_provider_auth"}))
        self.assertTrue(_worker_result_should_retry({"returncode": 1, "blocker_kind": "worker_no_diff"}))

    def test_worker_stream_disables_empty_event_cap_by_default(self) -> None:
        cfg = SimpleNamespace(env={})

        self.assertEqual(_engine_max_events_without_text(cfg, "worker-1"), 0)
        self.assertEqual(_engine_max_events_without_text(cfg, "manager"), 0)

    def test_empty_event_cap_can_be_configured(self) -> None:
        cfg = SimpleNamespace(env={"ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT": "12"})

        self.assertEqual(_engine_max_events_without_text(cfg, "worker-1"), 12)

    def test_docs_are_support_only_when_code_targets_exist(self) -> None:
        subtask = {
            "title": "Add tandem-tools tests",
            "files": [
                "crates/tandem-tools/src/lib_parts/part01.rs",
                "TESTING_UPDATES.md",
                "SECURITY.md",
                ".github/workflows/ci.yml",
            ],
        }

        self.assertEqual(
            _substantive_target_files(subtask),
            ["crates/tandem-tools/src/lib_parts/part01.rs"],
        )
        self.assertTrue(_support_only_changed_files_for_subtask(subtask, ["TESTING_UPDATES.md"]))
        self.assertFalse(
            _support_only_changed_files_for_subtask(
                subtask,
                ["crates/tandem-tools/src/lib_parts/part01.rs"],
            )
        )

    def test_ci_workflow_diff_satisfies_ci_gate_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            source = worktree / "crates" / "tandem-tools" / "src" / "lib.rs"
            source.parent.mkdir(parents=True)
            source.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "crates/tandem-tools/src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            workflow = worktree / ".github" / "workflows" / "tandem-tools-pr.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: tandem-tools\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: cargo test -p tandem-tools\n",
                encoding="utf-8",
            )

            result = _coerce_worker_failure(
                {
                    "returncode": 0,
                    "stdout": "Changed .github/workflows/tandem-tools-pr.yml and verified the cargo test command.\n",
                },
                log_path,
                worktree,
                {
                    "title": "Add tandem-tools test command to the PR CI gate",
                    "goal": "Ensure the tandem-tools test suite runs in CI for pull requests.",
                    "acceptance_criteria": [
                        "Update the existing CI PR workflow to run cargo test -p tandem-tools on a Linux runner.",
                    ],
                    "files": ["crates/tandem-tools/src/lib.rs"],
                },
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 0)
            self.assertNotEqual(result.get("failure_reason"), "TARGET_FILES_UNCHANGED")

    def test_write_required_worker_async_timeouts_are_capped(self) -> None:
        cfg = SimpleNamespace(env={})

        self.assertEqual(_async_prompt_timeout_seconds(cfg, "worker-1", True), 120.0)
        self.assertEqual(_async_no_text_timeout_seconds(cfg, "worker-1", True), 60.0)
        self.assertEqual(_async_prompt_timeout_seconds(cfg, "manager", False), 240.0)
        self.assertEqual(_async_no_text_timeout_seconds(cfg, "manager", False), 210.0)

    def test_write_required_worker_prompt_sync_timeout_is_capped(self) -> None:
        cfg = SimpleNamespace(env={})

        self.assertEqual(_scaled_prompt_sync_timeout_seconds(cfg, "worker-1", True, 2.85), 480.0)
        self.assertEqual(_scaled_async_prompt_timeout_seconds(cfg, "worker-1", True, 2.85), 120.0)
        self.assertEqual(_scaled_async_no_text_timeout_seconds(cfg, "worker-1", True, 2.85), 60.0)
        self.assertEqual(_scaled_prompt_sync_timeout_seconds(cfg, "manager", False, 2.0), 180.0)
        self.assertEqual(_scaled_async_prompt_timeout_seconds(cfg, "manager", False, 2.0), 480.0)
        self.assertEqual(_scaled_async_no_text_timeout_seconds(cfg, "manager", False, 2.0), 420.0)

    def test_merged_worker_timeout_multiplier_scales_with_contract_size(self) -> None:
        self.assertEqual(_worker_timeout_multiplier({"files": ["src/lib.rs"]}), 1.0)
        self.assertGreater(
            _worker_timeout_multiplier(
                {
                    "merged_subtasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    "files": [
                        "Cargo.toml",
                        "crates/tandem-meta-harness-eval/src/lib.rs",
                        "crates/tandem-meta-harness-eval/src/trace.rs",
                        "crates/tandem-meta-harness-eval/src/version.rs",
                    ],
                }
            ),
            1.0,
        )
        self.assertGreater(
            _worker_timeout_multiplier(
                {
                    "files": [
                        "Cargo.toml",
                        "crates/tandem-meta-harness-eval/Cargo.toml",
                        "crates/tandem-meta-harness-eval/src/lib.rs",
                        "crates/tandem-meta-harness-eval/src/trace.rs",
                        "crates/tandem-meta-harness-eval/src/scoring.rs",
                    ],
                }
            ),
            1.0,
        )

    def test_targeted_fixture_coverage_subtask_requires_real_diff(self) -> None:
        self.assertTrue(
            _subtask_requires_real_diff(
                {
                    "title": "Add focused Bug Monitor signal-gate fixture coverage",
                    "goal": "Extend the existing fixture coverage",
                    "files": ["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
                    "acceptance_criteria": [
                        "Fixture coverage includes blocked signal cases.",
                        "Assertions prove blocked fixtures do not create new draft work.",
                    ],
                }
            )
        )

    def test_read_only_targeted_subtask_does_not_require_real_diff(self) -> None:
        self.assertFalse(
            _subtask_requires_real_diff(
                {
                    "title": "Inspect current Bug Monitor behavior",
                    "goal": "Report whether the existing fixture already satisfies the gate.",
                    "files": ["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
                    "acceptance_criteria": ["Return findings and exact next operator action."],
                }
            )
        )

    def test_verification_first_repair_does_not_require_new_real_diff(self) -> None:
        self.assertFalse(
            _subtask_requires_real_diff(
                {
                    "title": "Finish repository worktree isolation and conflict detection patch",
                    "goal": "Verify the preserved source+test partial worker diff and fix only narrow verification failures.",
                    "files": [
                        "src/tandem_agents/core/repository/repository.py",
                        "src/tandem_agents/core/repository/repository_test.py",
                    ],
                    "acceptance_criteria": [
                        "Run the narrow deterministic verification first; if it passes, return a terminal completion note without making another mandatory edit.",
                        "If verification fails, fix only the failing behavior and rerun verification.",
                    ],
                    "repair_verification_first": True,
                }
            )
        )

    def test_empty_transcript_retry_prompt_is_role_aware(self) -> None:
        manager_prompt = _empty_transcript_retry_prompt(role="manager", write_required=False)
        worker_prompt = _empty_transcript_retry_prompt(role="worker-1", require_tool_use=True, write_required=True)

        self.assertIn("Return JSON only", manager_prompt)
        self.assertIn("Continue the original task using repository tools", worker_prompt)
        self.assertIn("produced and verified filesystem changes", worker_prompt)

    def test_call_with_timeout_raises_for_stalled_operation(self) -> None:
        import time

        with self.assertRaises(TimeoutError):
            _call_with_timeout(lambda: time.sleep(0.2), timeout_seconds=0.01)

    def test_seeded_pr_candidate_recovery_marks_worker_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            log_path.write_text("ENGINE_TOOL_LOOP_STALLED\n", encoding="utf-8")

            result = _recover_seeded_pr_candidate_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                {
                    "number": "1459",
                    "ref": "refs/aca/pr-1459",
                    "changed_files": ["packages/tandem-control-panel/src/pages/DashboardPage.tsx"],
                    "diff_stat": "M packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                },
                log_path,
            )

        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["recovered_from_pr_candidate_seed"])
        self.assertNotIn("failure_reason", result)
        self.assertNotIn("blocker_kind", result)

    def test_materialize_worker_context_copies_pr_artifact_inside_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "pr_candidate_context.json"
            artifact.write_text('{"pull_requests":[{"number":1459}]}\n', encoding="utf-8")
            worktree = root / "worktree"
            worktree.mkdir()

            prepared = _materialize_worker_context(
                worktree,
                {
                    "id": "subtask-1",
                    "pr_candidate_context_artifact": str(artifact),
                    "pr_candidate_context": [{"number": 1459}],
                },
            )

            self.assertEqual(prepared["pr_candidate_context_artifact"], ".aca/pr_candidate_context.json")
            self.assertTrue((worktree / ".aca" / "pr_candidate_context.json").exists())

    def test_retry_suffix_for_pr_context_requires_diff_or_structured_blocker(self) -> None:
        suffix = _worker_prompt_retry_suffix(
            {
                "id": "subtask-1",
                "files": ["src/lib/utils.ts"],
                "pr_candidate_context": [{"number": 1459}],
                "pr_candidate_refs": [{"number": 1459, "ok": True, "ref": "refs/aca/pr-1459"}],
            }
        )

        self.assertIn("Read `.aca/pr_candidate_context.json`", suffix)
        self.assertIn("Do not produce only an applicability matrix", suffix)
        self.assertIn("filesystem diff", suffix)
        self.assertIn("no-safe-changes blocker", suffix)
        self.assertNotIn("Start with `pwd`", suffix)

    def test_retry_suffix_for_target_files_requires_edit_before_blocker(self) -> None:
        suffix = _worker_prompt_retry_suffix(
            {
                "id": "subtask-1",
                "files": ["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
            }
        )

        self.assertIn("Missing test coverage or missing behavior is not a blocker", suffix)
        self.assertIn("Do not create marker files", suffix)
        self.assertIn("retry a narrower readback if a tool is skipped", suffix)
        self.assertIn("python3 -m unittest", suffix)
        self.assertIn("Do not treat missing `pytest` as a blocker", suffix)
        self.assertIn("Do not reply with `changed_files: []`", suffix)
        self.assertIn("Do not claim tool access is disallowed", suffix)
        self.assertNotIn("no-safe-changes blocker naming every inspected target file", suffix)

    def test_seedable_pr_candidate_specs_only_include_current_code_refs(self) -> None:
        specs = _seedable_pr_candidate_specs(
            {
                "files": ["src/current.ts", "scripts/ci-file-size-check.sh", "src/missing.ts"],
                "pr_candidate_refs": [
                    {"number": 1459, "ok": True, "ref": "refs/aca/pr-1459"},
                    {"number": 1414, "ok": True, "ref": "refs/aca/pr-1414"},
                    {"number": 1449, "ok": True, "ref": "refs/aca/pr-1449"},
                ],
                "pr_candidate_context": [
                    {
                        "number": 1459,
                        "files": [
                            {
                                "filename": "src/current.ts",
                                "base_path_exists": True,
                                "current_layout_stale": False,
                            }
                        ],
                    },
                    {
                        "number": 1414,
                        "files": [
                            {
                                "filename": "scripts/ci-file-size-check.sh",
                                "base_path_exists": True,
                                "current_layout_stale": False,
                            }
                        ],
                    },
                    {
                        "number": 1449,
                        "files": [
                            {
                                "filename": ".jules/bolt.md",
                                "base_path_exists": False,
                                "current_layout_stale": True,
                            },
                            {
                                "filename": "src/missing.ts",
                                "base_path_exists": True,
                                "current_layout_stale": False,
                            },
                        ],
                    },
                ],
            }
        )

        self.assertEqual(
            specs,
            [
                {"number": "1459", "ref": "refs/aca/pr-1459", "files": ["src/current.ts"], "skipped_files": []},
                {
                    "number": "1449",
                    "ref": "refs/aca/pr-1449",
                    "files": ["src/missing.ts"],
                    "skipped_files": [
                        {"path": ".jules/bolt.md", "reason": "excluded_generated_or_private_file"}
                    ],
                },
            ],
        )

    def test_seed_pr_candidate_diff_applies_first_safe_candidate_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            log_path = logs / "worker.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            (worktree / "src").mkdir()
            target = worktree / "src" / "current.ts"
            target.write_text("export const value = Math.max(...items.map((item) => item.count), 0);\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "branch", "main"], cwd=worktree, check=True)
            target.write_text("export const value = items.reduce((max, item) => Math.max(max, item.count), 0);\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "update-ref", "refs/aca/pr-1459", "HEAD"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)

            seeded = _seed_pr_candidate_diff(
                worktree,
                {
                    "files": ["src/current.ts", "scripts/ci-file-size-check.sh"],
                    "pr_candidate_refs": [
                        {"number": 1459, "ok": True, "ref": "refs/aca/pr-1459"},
                        {"number": 1414, "ok": True, "ref": "refs/aca/pr-1414"},
                    ],
                    "pr_candidate_context": [
                        {
                            "number": 1459,
                            "files": [
                                {
                                    "filename": "src/current.ts",
                                    "base_path_exists": True,
                                    "current_layout_stale": False,
                                }
                            ],
                        },
                        {
                            "number": 1414,
                            "files": [
                                {
                                    "filename": "scripts/ci-file-size-check.sh",
                                    "base_path_exists": True,
                                    "current_layout_stale": False,
                                },
                                {
                                    "filename": "src/old.ts",
                                    "base_path_exists": False,
                                    "current_layout_stale": True,
                                },
                            ],
                        }
                    ],
                },
                log_path,
            )

            self.assertIsNotNone(seeded)
            self.assertEqual(seeded["number"], "1459")
            self.assertEqual(seeded["numbers"], ["1459"])
            self.assertEqual(seeded["skipped_candidates"][0]["number"], "1414")
            self.assertIn("unsupported_file_type", seeded["skipped_candidates"][0]["reason"])
            self.assertIn("stale_or_missing_current_layout", seeded["skipped_candidates"][0]["reason"])
            self.assertEqual(_worktree_changed_files(worktree), ["src/current.ts"])
            self.assertIn("reduce", target.read_text(encoding="utf-8"))

    def test_seed_pr_candidate_diff_applies_multiple_safe_candidate_patches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            log_path = logs / "worker.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            (worktree / "src").mkdir()
            first = worktree / "src" / "current.ts"
            second = worktree / "src" / "other.ts"
            first.write_text("export const value = Math.max(...items.map((item) => item.count), 0);\n", encoding="utf-8")
            second.write_text("export const newest = items[items.length - 1];\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts", "src/other.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "branch", "main"], cwd=worktree, check=True)

            subprocess.run(["git", "checkout", "-b", "candidate-one", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            first.write_text("export const value = items.reduce((max, item) => Math.max(max, item.count), 0);\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate-one"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "update-ref", "refs/aca/pr-1459", "HEAD"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)

            subprocess.run(["git", "checkout", "-b", "candidate-two", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            second.write_text("export const newest = items.at(-1);\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/other.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate-two"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "update-ref", "refs/aca/pr-1414", "HEAD"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)

            seeded = _seed_pr_candidate_diff(
                worktree,
                {
                    "files": ["src/current.ts", "src/other.ts"],
                    "pr_candidate_refs": [
                        {"number": 1459, "ok": True, "ref": "refs/aca/pr-1459"},
                        {"number": 1414, "ok": True, "ref": "refs/aca/pr-1414"},
                    ],
                    "pr_candidate_context": [
                        {
                            "number": 1459,
                            "files": [
                                {
                                    "filename": "src/current.ts",
                                    "base_path_exists": True,
                                    "current_layout_stale": False,
                                }
                            ],
                        },
                        {
                            "number": 1414,
                            "files": [
                                {
                                    "filename": "src/other.ts",
                                    "base_path_exists": True,
                                    "current_layout_stale": False,
                                },
                                {
                                    "filename": ".jules/bolt.md",
                                    "base_path_exists": False,
                                    "current_layout_stale": True,
                                },
                            ],
                        },
                    ],
                },
                log_path,
            )

            self.assertIsNotNone(seeded)
            self.assertEqual(seeded["numbers"], ["1459", "1414"])
            self.assertEqual(
                _worktree_changed_files(worktree),
                ["src/current.ts", "src/other.ts"],
            )
            self.assertIn("reduce", first.read_text(encoding="utf-8"))
            self.assertIn("at(-1)", second.read_text(encoding="utf-8"))
            self.assertEqual(
                seeded["candidates"][1]["skipped_files"],
                [{"path": ".jules/bolt.md", "reason": "excluded_generated_or_private_file"}],
            )

    def test_seed_pr_candidate_diff_rejects_missing_relative_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "logs"
            logs.mkdir()
            log_path = logs / "worker.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            (worktree / "src").mkdir()
            target = worktree / "src" / "current.ts"
            target.write_text("export const value = 1;\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "branch", "main"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "-b", "candidate", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("import { missing } from './missing';\nexport const value = missing();\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/current.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "candidate"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "update-ref", "refs/aca/pr-1449", "HEAD"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "main"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)

            seeded = _seed_pr_candidate_diff(
                worktree,
                {
                    "files": ["src/current.ts"],
                    "pr_candidate_refs": [{"number": 1449, "ok": True, "ref": "refs/aca/pr-1449"}],
                    "pr_candidate_context": [
                        {
                            "number": 1449,
                            "files": [
                                {
                                    "filename": "src/current.ts",
                                    "base_path_exists": True,
                                    "current_layout_stale": False,
                                }
                            ],
                        }
                    ],
                },
                log_path,
            )

            self.assertIsNone(seeded)
            self.assertEqual(_worktree_changed_files(worktree), [])
            self.assertIn("missing relative import", log_path.read_text(encoding="utf-8"))

    def test_run_worker_subtask_syncs_successful_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "run"
            worktree = root / "worktree"
            repo_path.mkdir()
            worktree.mkdir()
            cfg = SimpleNamespace()
            result = {
                "returncode": 0,
                "stdout": "changed files",
                "log_path": str(run_dir / "logs" / "worker-1.log"),
            }

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_preflight", return_value=(True, "ok")), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5", "source": "engine_default"},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.engine_env", return_value={}), \
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt", return_value=result), \
                mock.patch("src.tandem_agents.core.execution.worker._coerce_worker_failure", return_value=result), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worker_artifacts"), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worktree_changes") as sync_changes, \
                mock.patch("src.tandem_agents.core.execution.worker.summarize_worker_notes", return_value={"returncode": 0}):
                sync_changes.return_value = ["src/lib.rs"]
                output = run_worker_subtask(
                    cfg,
                    "run-1",
                    repo_path,
                    run_dir,
                    {"task_id": "TAN-111", "source": {"type": "linear"}, "title": "Task"},
                    {"id": "subtask-1", "title": "Subtask", "goal": "Change files", "files": []},
                    "worker-1",
                    1,
                )

            self.assertEqual(output["returncode"], 0)
            sync_changes.assert_called_once_with(worktree, repo_path)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            completed = next(event for event in events if event["type"] == "worker.completed")
            self.assertEqual(completed["payload"]["changed_files"], ["src/lib.rs"])
            self.assertEqual(completed["payload"]["synced_files"], ["src/lib.rs"])

    def test_run_worker_subtask_reports_failed_retry_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "run"
            worktree = root / "worktree"
            repo_path.mkdir()
            worktree.mkdir()
            cfg = SimpleNamespace()
            first_result = {
                "returncode": 1,
                "stdout": "first failure",
                "failure_reason": "NO_FILESYSTEM_CHANGES",
                "blocker_kind": "worker_no_diff",
                "log_path": str(run_dir / "logs" / "worker-1.log"),
            }
            retry_result = {
                "returncode": 1,
                "stdout": "retry timeout",
                "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                "blocker_kind": "engine_prompt_timeout",
                "partial_diff_artifact": str(run_dir / "artifacts" / "worker-1.partial-worker-diff.patch"),
                "log_path": str(run_dir / "logs" / "worker-1.log"),
            }

            def summarize(result: dict[str, object], *_args: object) -> dict[str, object]:
                return dict(result)

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_preflight", return_value=(True, "ok")), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5", "source": "engine_default"},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.engine_env", return_value={}), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.stream_tandem_prompt",
                    side_effect=[first_result, retry_result],
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker._terminalize_worker_after_tool_loop",
                    side_effect=lambda _cfg, result, *_args, **_kwargs: result,
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker._coerce_worker_failure",
                    side_effect=lambda result, *_args, **_kwargs: result,
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker._recover_nonzero_result_if_diff_satisfies_subtask",
                    side_effect=lambda result, *_args, **_kwargs: result,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worker_artifacts"), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worktree_changes") as sync_changes, \
                mock.patch("src.tandem_agents.core.execution.worker.summarize_worker_notes", side_effect=summarize):
                output = run_worker_subtask(
                    cfg,
                    "run-1",
                    repo_path,
                    run_dir,
                    {"task_id": "TAN-216", "source": {"type": "linear"}, "title": "Task"},
                    {"id": "subtask-1", "title": "Subtask", "goal": "Change files", "files": ["src/lib.rs"]},
                    "worker-1",
                    1,
                )

            self.assertEqual(output["returncode"], 1)
            self.assertEqual(output["failure_reason"], "ENGINE_PROMPT_TIMEOUT")
            self.assertEqual(output["blocker_kind"], "engine_prompt_timeout")
            self.assertEqual(output["partial_diff_artifact"], retry_result["partial_diff_artifact"])
            sync_changes.assert_not_called()
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_types = [event["type"] for event in events]
            self.assertIn("worker.retry_started", event_types)
            self.assertIn("worker.retry_completed", event_types)
            self.assertIn("worker.partial_diff_preserved", event_types)
            retry_completed = next(event for event in events if event["type"] == "worker.retry_completed")
            self.assertEqual(retry_completed["payload"]["partial_diff_state"], "preserved_not_accepted")
            self.assertEqual(
                retry_completed["payload"]["partial_diff_artifact"],
                retry_result["partial_diff_artifact"],
            )

    def test_worker_incomplete_diff_defers_retry_to_outer_repair_loop(self) -> None:
        self.assertFalse(
            _worker_result_should_retry(
                {
                    "returncode": 1,
                    "blocker_kind": "worker_incomplete_diff",
                    "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                }
            )
        )

    def test_late_worker_result_after_terminal_run_does_not_sync_or_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "run"
            worktree = root / "worktree"
            repo_path.mkdir()
            worktree.mkdir()
            cfg = SimpleNamespace(env={})

            def terminal_stream(*_args: object, **kwargs: object) -> dict[str, object]:
                (run_dir / "status.json").write_text(
                    json.dumps({"run": {"status": "blocked", "completed_at_ms": 123}}),
                    encoding="utf-8",
                )
                return {
                    "returncode": 0,
                    "stdout": "late success",
                    "log_path": str(kwargs["log_path"]),
                }

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_preflight", return_value=(True, "ok")), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5", "source": "engine_default"},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.engine_env", return_value={}), \
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt", side_effect=terminal_stream), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worker_artifacts") as sync_artifacts, \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worktree_changes") as sync_changes, \
                mock.patch("src.tandem_agents.core.execution.worker.git_diff_stat", return_value=""), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_changed_files", return_value=[]):
                output = run_worker_subtask(
                    cfg,
                    "run-1",
                    repo_path,
                    run_dir,
                    {"task_id": "TAN-216", "source": {"type": "linear"}, "title": "Task"},
                    {"id": "subtask-1", "title": "Subtask", "goal": "Change files", "files": ["src/lib.rs"]},
                    "worker-1",
                    1,
                )

            self.assertEqual(output["returncode"], 1)
            self.assertEqual(output["blocker_kind"], "stale_worker_result")
            sync_artifacts.assert_not_called()
            sync_changes.assert_not_called()
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            event_types = [event["type"] for event in events]
            self.assertEqual(event_types, ["worker.started"])

    def test_superseded_worker_attempt_does_not_sync_or_emit_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            repo_path = root / "repo"
            worktree = root / "worktree"
            run_dir.mkdir()
            repo_path.mkdir()
            worktree.mkdir()
            cfg = SimpleNamespace(env={})
            (run_dir / "status.json").write_text(
                json.dumps({"run": {"status": "running"}}),
                encoding="utf-8",
            )

            def superseded_stream(*_args: object, **kwargs: object) -> dict[str, object]:
                (run_dir / "active_worker_attempts.json").write_text(
                    json.dumps({"worker-1": "newer-exec"}),
                    encoding="utf-8",
                )
                return {
                    "returncode": 0,
                    "stdout": "late success",
                    "log_path": str(kwargs["log_path"]),
                }

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_preflight", return_value=(True, "ok")), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_session_provider_model",
                    return_value={"provider": "openai-codex", "model": "gpt-5.5", "source": "engine_default"},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.engine_env", return_value={}), \
                mock.patch("src.tandem_agents.core.execution.worker.stream_tandem_prompt", side_effect=superseded_stream), \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worker_artifacts") as sync_artifacts, \
                mock.patch("src.tandem_agents.core.execution.worker.sync_worktree_changes") as sync_changes, \
                mock.patch("src.tandem_agents.core.execution.worker.summarize_worker_notes", side_effect=lambda result, *_args: result):
                output = run_worker_subtask(
                    cfg,
                    "run-1",
                    repo_path,
                    run_dir,
                    {"task_id": "TAN-216", "source": {"type": "linear"}, "title": "Task"},
                    {
                        "id": "subtask-1",
                        "_worker_execution_id": "old-exec",
                        "title": "Subtask",
                        "goal": "Change files",
                        "files": ["src/lib.rs"],
                    },
                    "worker-1",
                    1,
                )

            self.assertEqual(output["returncode"], 1)
            self.assertEqual(output["blocker_kind"], "stale_worker_result")
            self.assertEqual(output["failure_reason"], "WORKER_RESULT_AFTER_RETRY_SUPERSEDED")
            sync_artifacts.assert_not_called()
            sync_changes.assert_not_called()
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([event["type"] for event in events], ["worker.started"])
            self.assertEqual(events[0]["payload"]["execution_id"], "old-exec")

    def test_engine_empty_response_failure_gets_actionable_blocker_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_EMPTY_RESPONSE: empty\n",
                    "failure_reason": "ENGINE_EMPTY_RESPONSE",
                },
                log_path,
                worktree,
                {"files": []},
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_empty_response")

    def test_engine_provider_auth_error_gets_actionable_blocker_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_ERROR: ENGINE_DISPATCH_FAILED: You didn't provide an API key.\n",
                },
                log_path,
                worktree,
                {"files": []},
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_provider_auth")

    def test_engine_dispatch_failure_with_target_diff_enters_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            worktree.mkdir()
            log_path = root / "worker.log"
            log_path.write_text("", encoding="utf-8")
            (worktree / "src").mkdir()
            target = worktree / "src" / "existing.ts"
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            subprocess.run(["git", "add", "src/existing.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("after\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_ERROR: ENGINE_DISPATCH_FAILED: iteration budget\n",
                },
                log_path,
                worktree,
                {"files": ["src/existing.ts", "src/stale.ts"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["recovered_success"])
            self.assertTrue(result["recovered_from_engine_dispatch_partial_diff"])
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertNotIn("blocker_kind", result)
            self.assertNotIn("partial_diff_artifact", result)
            self.assertIn("normal integration review", log_path.read_text(encoding="utf-8"))

    def test_tool_loop_stall_with_diff_recovers_into_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "existing.ts"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/existing.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("after\n", encoding="utf-8")

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertIn("engine_tool_loop_stalled_partial_diff", result["warnings"])
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())

    def test_tool_loop_with_target_diff_terminalizes_with_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "existing.ts"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/existing.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("after\n", encoding="utf-8")

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                return_value="terminal-session",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker._prompt_sync_with_connect_retries",
                return_value={
                    "messages": [
                        {
                            "info": {"role": "assistant"},
                            "parts": [{"text": "Changed src/existing.ts. Verification: inspected diff and target file."}],
                        }
                    ]
                },
            ) as prompt_sync:
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    {
                        "returncode": 1,
                        "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                        "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                        "blocker_kind": "engine_tool_loop_stalled",
                        "engine": {"session_id": "stalled-session"},
                    },
                    log_path,
                    worktree,
                    {"title": "Update target", "files": ["src/existing.ts"]},
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["terminalized_after_tool_loop"])
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertNotIn("failure_reason", result)
            self.assertIn("ENGINE_TOOL_LOOP_TERMINALIZED", result["stdout"])
            kwargs = prompt_sync.call_args.kwargs
            self.assertEqual(kwargs["tool_allowlist"], [])
            self.assertEqual(kwargs["tool_mode"], "none")
            self.assertFalse(kwargs["write_required"])

    def test_tool_loop_terminalize_skips_deferred_partial_diff_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "existing.py"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/existing.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("after\n", encoding="utf-8")

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session"
            ) as create_session:
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    {
                        "returncode": 1,
                        "stdout": "ENGINE_PROMPT_TIMEOUT\n",
                        "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                        "blocker_kind": "engine_prompt_timeout",
                        "engine": {
                            "session_id": "stalled-session",
                            "partial_diff_recovery_deferred": True,
                        },
                    },
                    log_path,
                    worktree,
                    {"title": "Update target", "files": ["src/existing.py"]},
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_prompt_timeout")
            self.assertNotIn("terminalized_after_tool_loop", result)
            create_session.assert_not_called()

    def test_tool_loop_terminalize_keeps_remaining_blockers_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "existing.ts"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/existing.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("after\n", encoding="utf-8")

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                return_value="terminal-session",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker._prompt_sync_with_connect_retries",
                return_value={
                    "messages": [
                        {
                            "info": {"role": "assistant"},
                            "parts": [
                                {
                                    "text": (
                                        "Changed src/existing.ts.\n"
                                        "Remaining blockers: path sandbox and approval classifier tests are missing."
                                    )
                                }
                            ],
                        }
                    ]
                },
            ):
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    {
                        "returncode": 1,
                        "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                        "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                        "blocker_kind": "engine_tool_loop_stalled",
                        "engine": {"session_id": "stalled-session"},
                    },
                    log_path,
                    worktree,
                    {"title": "Update target", "files": ["src/existing.ts"]},
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "TERMINALIZED_WITH_REMAINING_BLOCKERS")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertIn("Remaining blockers", result["stdout"])

    def test_tool_loop_terminalize_ignores_unrelated_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "existing.ts"
            target.parent.mkdir()
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/existing.ts"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            note = worktree / "docs" / "note.md"
            note.parent.mkdir()
            note.write_text("after\n", encoding="utf-8")

            original = {
                "returncode": 1,
                "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                "blocker_kind": "engine_tool_loop_stalled",
            }
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session") as create_session:
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    original,
                    log_path,
                    worktree,
                    {"title": "Update target", "files": ["src/existing.ts"]},
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertIs(result, original)
            create_session.assert_not_called()

    def test_tool_loop_terminalizes_nearby_crate_test_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "crates" / "tandem-tools" / "src" / "lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "crates/tandem-tools/src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            test_file = worktree / "crates" / "tandem-tools" / "tests" / "security_suite.rs"
            test_file.parent.mkdir(parents=True)
            test_file.write_text("#[test]\nfn covers_security_contract() {}\n", encoding="utf-8")

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                return_value="terminal-session",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker._prompt_sync_with_connect_retries",
                return_value={
                    "messages": [
                        {
                            "info": {"role": "assistant"},
                            "parts": [{"text": "Changed crate tests. Verification: cargo test -p tandem-tools passed."}],
                        }
                    ]
                },
            ):
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    {
                        "returncode": 1,
                        "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                        "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                        "blocker_kind": "engine_tool_loop_stalled",
                    },
                    log_path,
                    worktree,
                    {
                        "title": "Add unit test suite for tandem-tools",
                        "goal": "Add tests for tandem-tools registry resolution and approval classifier.",
                        "files": ["crates/tandem-tools/src/lib.rs"],
                        "acceptance_criteria": ["cargo test -p tandem-tools passes"],
                    },
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["terminalized_after_tool_loop"])
            self.assertEqual(result["changed_files"], ["crates/tandem-tools/tests/security_suite.rs"])
            self.assertNotIn("failure_reason", result)

    def test_tool_loop_terminalize_rejects_unverified_test_only_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "crates" / "tandem-server" / "src" / "http" / "coder_parts" / "part09.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            test_file = (
                worktree
                / "crates"
                / "tandem-server"
                / "src"
                / "http"
                / "tests"
                / "coder_parts"
                / "part09.rs"
            )
            test_file.parent.mkdir(parents=True)
            test_file.write_text("#[tokio::test]\nasync fn existing_test() {}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "crates/tandem-server/src/http/coder_parts/part09.rs", "crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
                cwd=worktree,
                check=True,
            )
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            test_file.write_text(
                test_file.read_text(encoding="utf-8")
                + "\n#[test]\nfn github_projects_readiness_diagnostics_cover_drift() {\n"
                + "    let diagnostic = github_projects_schema_drift_readiness_diagnostic(\"ProjectV2 drift\");\n"
                + "    assert!(diagnostic.contains(\"read readiness\"));\n"
                + "}\n",
                encoding="utf-8",
            )

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                return_value="terminal-session",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker._prompt_sync_with_connect_retries",
                return_value={
                    "messages": [
                        {
                            "info": {"role": "assistant"},
                            "parts": [
                                {
                                    "text": (
                                        "Changed crate tests.\n"
                                        "Verification: verification not run.\n"
                                        "Remaining implementation blockers: none visible from the diff."
                                    )
                                }
                            ],
                        }
                    ]
                },
            ):
                result = _terminalize_worker_after_tool_loop(
                    SimpleNamespace(env={}),
                    {
                        "returncode": 1,
                        "stdout": "ENGINE_PROMPT_TIMEOUT\n",
                        "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                        "blocker_kind": "engine_prompt_timeout",
                    },
                    log_path,
                    worktree,
                    {
                        "title": "Add GitHub Projects schema drift and divergence regression coverage",
                        "goal": "Harden GitHub Projects intake against schema drift and remote state changes.",
                        "files": ["crates/tandem-server/src/http/coder_parts/part09.rs"],
                        "acceptance_criteria": [
                            "Tests cover schema drift, remote divergence, reopened terminal items, and degraded write capability.",
                            "Regression output identifies degraded read/write readiness clearly.",
                        ],
                    },
                    role="worker-1",
                    provider="openai-codex",
                    model="gpt-5.5",
                    require_filesystem_changes=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "TERMINALIZED_UNVERIFIED_TEST_ONLY_DIFF")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertEqual(
                result["changed_files"],
                ["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
            )
            self.assertIn("rejected the terminalized worker note", result["stdout"])
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())
            self.assertNotIn("terminalized_after_tool_loop", result)

    def test_private_helper_subtask_does_not_accept_nearby_integration_test_only_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo"
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "crates" / "tandem-tools" / "src" / "lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub(crate) fn resolve_registered_tool() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "crates/tandem-tools/src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            test_file = worktree / "crates" / "tandem-tools" / "tests" / "registry_resolution.rs"
            test_file.parent.mkdir(parents=True)
            test_file.write_text("#[test]\nfn registry_resolution_cases() {}\n", encoding="utf-8")

            self.assertFalse(
                _diff_touches_nearby_test_files(
                    worktree,
                    {
                        "title": "Registry resolution unit tests",
                        "goal": "Cover resolve_registered_tool private helper behavior.",
                        "files": ["crates/tandem-tools/src/lib.rs"],
                        "acceptance_criteria": ["Add table-driven tests for resolve_registered_tool."],
                    },
                )
            )

    def test_regression_task_rejects_source_local_helper_tests_without_declared_test_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo"
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            source = worktree / "crates" / "tandem-server" / "src" / "http" / "coder_parts" / "part09.rs"
            test_target = worktree / "crates" / "tandem-server" / "src" / "http" / "tests" / "coder_parts" / "part09.rs"
            source.parent.mkdir(parents=True)
            test_target.parent.mkdir(parents=True)
            source.write_text("pub(super) async fn coder_project_run_create() {}\n", encoding="utf-8")
            test_target.write_text("#[test]\nfn existing_part09_test() {}\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "add",
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ],
                cwd=worktree,
                check=True,
            )
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            source.write_text(
                "\n".join(
                    [
                        "pub(super) fn github_projects_readiness_schema_drift_message(cause: &str) -> String {",
                        "    format!(\"GitHub Projects read readiness degraded: {cause}\")",
                        "}",
                        "",
                        "#[cfg(test)]",
                        "mod github_projects_readiness_tests {",
                        "    use super::github_projects_readiness_schema_drift_message;",
                        "",
                        "    #[test]",
                        "    fn schema_drift_message_identifies_degraded_readiness() {",
                        "        let message = github_projects_readiness_schema_drift_message(\"missing field\");",
                        "        assert!(message.contains(\"read readiness degraded\"));",
                        "    }",
                        "}",
                        "",
                        "pub(super) async fn coder_project_run_create() {}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 0,
                    "stdout": "Changed part09.rs. Verification not run.",
                },
                log_path,
                worktree,
                {
                    "title": "Add schema drift readiness regression",
                    "goal": "Verify GitHub Projects read readiness degrades clearly when schema drift occurs.",
                    "files": [
                        "crates/tandem-server/src/http/coder_parts/part09.rs",
                        "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                    ],
                    "acceptance_criteria": [
                        "A regression test simulates GitHub Projects schema drift using the existing test harness.",
                    ],
                },
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "SELF_REFERENTIAL_TEST_ONLY_DIFF")
            self.assertEqual(result["blocker_kind"], "worker_incomplete_diff")
            self.assertIn("declared test target", result["stdout"])

    def test_tool_loop_stall_with_repeated_source_diff_blocks_as_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            repeated = "//! Bug Monitor service orchestration validates signal gates before draft work."
            target.write_text(
                "\n".join([repeated, "pub fn existing() {}", repeated, repeated, repeated, repeated]) + "\n",
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_CORRUPT_DIFF")
            self.assertEqual(result["blocker_kind"], "worker_corrupt_diff")
            self.assertNotIn("recovered_from_engine_stall", result)
            self.assertIn("mechanically corrupted", result["stdout"])

    def test_success_with_repeated_source_diff_blocks_as_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            repeated = "//! Bug Monitor service orchestration validates signal gates before draft work."
            target.write_text(
                "\n".join([repeated, "pub fn existing() {}", repeated, repeated, repeated, repeated]) + "\n",
                encoding="utf-8",
            )

            result = _coerce_worker_failure(
                {"returncode": 0, "stdout": "done\n"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_CORRUPT_DIFF")
            self.assertEqual(result["blocker_kind"], "worker_corrupt_diff")
            self.assertIn("mechanically corrupted", result["stdout"])

    def test_tool_loop_stall_with_duplicate_python_top_level_def_blocks_as_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "repository.py"
            target.parent.mkdir()
            target.write_text("def existing() -> str:\n    return 'base'\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/repository.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text(
                "\n".join(
                    [
                        "def existing() -> str:",
                        "    return 'base'",
                        "",
                        "def issue_worktree_branch(issue_id: str) -> str:",
                        "    return issue_id.lower()",
                        "",
                        "def issue_worktree_branch(issue_id: str) -> str:",
                        "    return issue_id.strip().lower()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "WORKER_CORRUPT_DIFF")
            self.assertEqual(result["blocker_kind"], "worker_corrupt_diff")
            self.assertNotIn("partial_diff_preserved_after_engine_stall", result)
            self.assertIn("duplicate top-level Python definition", result["stdout"])

    def test_tool_loop_stall_with_duplicate_python_methods_is_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "repository.py"
            target.parent.mkdir()
            target.write_text("def existing() -> str:\n    return 'base'\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/repository.py"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text(
                "\n".join(
                    [
                        "def existing() -> str:",
                        "    return 'base'",
                        "",
                        "class First:",
                        "    def render(self) -> str:",
                        "        return 'first'",
                        "",
                        "class Second:",
                        "    def render(self) -> str:",
                        "        return 'second'",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")

    def test_tool_loop_stall_with_repeated_serde_attributes_is_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text(
                "use serde::{Deserialize, Serialize};\n\n#[derive(Serialize, Deserialize)]\npub struct Existing {}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            repeated = '    #[serde(default, skip_serializing_if = "Option::is_none")]'
            target.write_text(
                "\n".join(
                    [
                        "use serde::{Deserialize, Serialize};",
                        "",
                        "#[derive(Serialize, Deserialize)]",
                        "pub struct Existing {}",
                        "",
                        "#[derive(Serialize, Deserialize)]",
                        "pub struct OperatorState {",
                        repeated,
                        "    pub evidence: Option<String>,",
                        repeated,
                        "    pub artifacts: Option<String>,",
                        repeated,
                        "    pub proposal_state: Option<String>,",
                        repeated,
                        "    pub approval_state: Option<String>,",
                        repeated,
                        "    pub published_output: Option<String>,",
                        "}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")

    def test_repeated_tempdir_cleanup_is_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            cleanup = "        fs::remove_dir_all(workspace).ok();"
            target.write_text(
                "\n".join(
                    [
                        "use std::fs;",
                        "pub fn existing() {}",
                        "#[cfg(test)]",
                        "mod tests {",
                        cleanup,
                        cleanup,
                        cleanup,
                        cleanup,
                        cleanup,
                        "}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")

    def test_repeated_test_fixture_constructor_is_not_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "src" / "lib.rs"
            target.parent.mkdir()
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            setup = '        let workspace = TestDir::new("workspace");'
            target.write_text(
                "\n".join(
                    [
                        "pub fn existing() {}",
                        "#[cfg(test)]",
                        "mod tests {",
                        "    struct TestDir;",
                        '    impl TestDir { fn new(_: &str) -> Self { Self } }',
                        setup,
                        setup,
                        setup,
                        setup,
                        setup,
                        "}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = _recover_nonzero_result_with_diff(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                reason="target file diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")

    def test_tool_loop_stall_with_unrelated_diff_does_not_satisfy_targeted_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "scripts" / "gate.test.mjs"
            target.parent.mkdir()
            target.write_text("console.log('base');\n", encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            note = worktree / "docs" / "handoff.md"
            note.parent.mkdir()
            note.write_text("verification checklist\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {
                    "title": "Add focused Bug Monitor signal-gate fixture coverage",
                    "files": ["scripts/gate.test.mjs"],
                },
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "target_files_unchanged")
            self.assertNotIn("recovered_from_engine_stall", result)
            self.assertEqual(result["changed_files"], ["docs/handoff.md"])
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())

    def test_success_with_unrelated_diff_blocks_targeted_write_required_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "scripts" / "gate.test.mjs"
            target.parent.mkdir()
            target.write_text("console.log('base');\n", encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            note = worktree / "docs" / "handoff.md"
            note.parent.mkdir()
            note.write_text("verification checklist\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 0, "stdout": "done\n"},
                log_path,
                worktree,
                {
                    "title": "Add focused Bug Monitor signal-gate fixture coverage",
                    "files": ["scripts/gate.test.mjs"],
                },
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "TARGET_FILES_UNCHANGED")
            self.assertEqual(result["blocker_kind"], "target_files_unchanged")
            self.assertIn("docs/handoff.md", result["stdout"])

    def test_late_target_diff_recovers_before_worker_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "scripts" / "gate.test.mjs"
            target.parent.mkdir()
            target.write_text("console.log('base');\n", encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("console.log('covered');\n", encoding="utf-8")

            result = _recover_nonzero_result_if_diff_satisfies_subtask(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {
                    "title": "Add focused Bug Monitor signal-gate fixture coverage",
                    "files": ["scripts/gate.test.mjs"],
                },
                require_filesystem_changes=True,
                reason="late target diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["changed_files"], ["scripts/gate.test.mjs"])

    def test_prompt_timeout_with_target_diff_recovers_before_worker_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            target = worktree / "scripts" / "gate.test.mjs"
            target.parent.mkdir()
            target.write_text("console.log('base');\n", encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target.write_text("console.log('covered');\n", encoding="utf-8")

            result = _recover_nonzero_result_if_diff_satisfies_subtask(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_PROMPT_TIMEOUT\n",
                    "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                    "blocker_kind": "engine_prompt_timeout",
                },
                log_path,
                worktree,
                {
                    "title": "Add focused Bug Monitor signal-gate fixture coverage",
                    "files": ["scripts/gate.test.mjs"],
                },
                require_filesystem_changes=True,
                reason="late target diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["changed_files"], ["scripts/gate.test.mjs"])

    def test_manifest_only_diff_does_not_satisfy_fixture_coverage_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            fixture = worktree / "scripts" / "gate.test.mjs"
            fixture.parent.mkdir()
            fixture.write_text("console.log('base');\n", encoding="utf-8")
            manifest = worktree / "package.json"
            manifest.write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs", "package.json"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            manifest.write_text('{"scripts":{"test":"node --test","test:gate":"node scripts/gate.test.mjs"}}\n', encoding="utf-8")

            result = _recover_nonzero_result_if_diff_satisfies_subtask(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {
                    "title": "Extend Bug Monitor fixture coverage",
                    "files": ["scripts/gate.test.mjs", "package.json"],
                    "acceptance_criteria": [
                        "Assertions cover blocked signal cases.",
                    ],
                },
                require_filesystem_changes=True,
                reason="late package diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertNotIn("recovered_from_engine_stall", result)

    def test_success_with_manifest_only_diff_blocks_fixture_coverage_subtask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            fixture = worktree / "scripts" / "gate.test.mjs"
            fixture.parent.mkdir()
            fixture.write_text("console.log('base');\n", encoding="utf-8")
            manifest = worktree / "package.json"
            manifest.write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            subprocess.run(["git", "add", "scripts/gate.test.mjs", "package.json"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            manifest.write_text('{"scripts":{"test":"node --test","test:gate":"node scripts/gate.test.mjs"}}\n', encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 0, "stdout": "done\n"},
                log_path,
                worktree,
                {
                    "title": "Extend Bug Monitor fixture coverage",
                    "files": ["scripts/gate.test.mjs", "package.json"],
                    "acceptance_criteria": [
                        "Assertions cover blocked signal cases.",
                    ],
                },
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "TARGET_FILES_UNCHANGED")
            self.assertEqual(result["blocker_kind"], "target_files_unchanged")
            self.assertIn("substantive target files", result["stdout"])
            self.assertIn("scripts/gate.test.mjs", result["stdout"])

    def test_manifest_only_subtask_can_recover_manifest_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            manifest = worktree / "package.json"
            manifest.write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
            subprocess.run(["git", "add", "package.json"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            manifest.write_text('{"scripts":{"test":"node --test","test:gate":"node scripts/gate.test.mjs"}}\n', encoding="utf-8")

            result = _recover_nonzero_result_if_diff_satisfies_subtask(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {
                    "title": "Add package script for Bug Monitor fixture smoke",
                    "files": ["package.json"],
                    "acceptance_criteria": [
                        "Documented command is runnable from package scripts.",
                    ],
                },
                require_filesystem_changes=True,
                reason="late manifest diff",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertTrue(result["partial_diff_preserved_after_engine_stall"])
            self.assertEqual(result["changed_files"], ["package.json"])

    def test_worker_changed_files_ignore_aca_blocker_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / "aca-subtask-1-blocker.md").write_text("blocked\n", encoding="utf-8")
            (worktree / ".aca").mkdir()
            (worktree / ".aca" / "pr_candidate_context.json").write_text("{}\n", encoding="utf-8")
            (worktree / "__aca_temp_probe.txt").write_text("placeholder\n", encoding="utf-8")

            self.assertEqual(_worktree_changed_files(worktree), [])

    def test_success_with_only_aca_blocker_artifact_becomes_no_diff_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / "aca-subtask-1-blocker.md").write_text("blocked\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 0, "stdout": "wrote blocker artifact\n"},
                log_path,
                worktree,
                {"files": ["src/app.ts"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "NO_FILESYSTEM_CHANGES")
            self.assertEqual(result["blocker_kind"], "worker_no_diff")

    def test_annotate_ignored_target_files_filters_reviewless_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")

            subtask = _annotate_ignored_target_files(
                worktree,
                {
                    "files": ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md", "scripts/smoke.mjs"],
                    "target_files": ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md", "scripts/smoke.mjs"],
                },
            )

            self.assertEqual(subtask["files"], ["scripts/smoke.mjs"])
            self.assertEqual(subtask["target_files"], ["scripts/smoke.mjs"])
            self.assertEqual(
                subtask["ignored_target_files"],
                ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md"],
            )

    def test_git_ignored_paths_uses_host_mapped_worktree_gitdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aca_root = root / "aca"
            host_root = root / "host"
            aca_root.mkdir()
            host_root.symlink_to(aca_root, target_is_directory=True)
            repo = aca_root / "workspace" / "repos" / "demo"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            (repo / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=ACA", "-c", "user.email=tandem-agents.invalid", "commit", "-m", "init"],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            worktree = aca_root / "runs" / "run-1" / "worktrees" / "worker-1"
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            git_file = worktree / ".git"
            gitdir = Path(git_file.read_text(encoding="utf-8").split(":", 1)[1].strip())
            host_gitdir = host_root / gitdir.relative_to(aca_root)
            git_file.write_text(f"gitdir: {host_gitdir}\n", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {
                    "ACA_ROOT": str(aca_root),
                    "ACA_ENGINE_HOST_ROOT": str(host_root),
                },
            ):
                ignored = _git_ignored_paths(
                    worktree,
                    ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md", "README.md"],
                )

        self.assertEqual(ignored, ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md"])

    def test_engine_stall_with_only_ignored_target_changes_reports_ignored_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            subprocess.run(["git", "add", ".gitignore"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            ignored = worktree / "docs" / "internal" / "SIGNAL_TRIAGE_PIPELINE_KANBAN.md"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("gap note\n", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {"files": ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "IGNORED_PATH_CHANGES")
            self.assertEqual(result["blocker_kind"], "ignored_path_changes")
            self.assertEqual(
                result["ignored_files"],
                ["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md"],
            )
            self.assertIn("Git-ignored target files", result["stdout"])

    def test_engine_timeout_with_metadata_only_diff_becomes_target_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            logs = root / "run" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "worker-1.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            scripts_dir = worktree / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "bug-monitor-external-log-intake-fixture.test.mjs").write_text(
                "test('existing', () => {});\n",
                encoding="utf-8",
            )
            (worktree / "package.json").write_text(
                '{\n  "scripts": {}\n}\n',
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "package.json", "scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
                cwd=worktree,
                check=True,
            )
            subprocess.run(["git", "commit", "-m", "baseline"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / "package.json").write_text(
                '{\n  "scripts": {"test:bug-monitor:fixture": "node --test scripts/bug-monitor-external-log-intake-fixture.test.mjs"}\n}\n',
                encoding="utf-8",
            )

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_PROMPT_TIMEOUT\n",
                    "failure_reason": "ENGINE_PROMPT_TIMEOUT",
                    "blocker_kind": "engine_prompt_timeout",
                },
                log_path,
                worktree,
                {
                    "title": "Add focused Bug Monitor signal quality-gate fixture coverage",
                    "goal": "Extend existing Bug Monitor external log intake tests",
                    "files": ["scripts/bug-monitor-external-log-intake-fixture.test.mjs", "package.json"],
                    "acceptance_criteria": ["Fixture coverage includes blocked quality gate assertions."],
                },
                require_filesystem_changes=False,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "TARGET_FILES_UNCHANGED")
            self.assertEqual(result["engine_failure_reason"], "ENGINE_PROMPT_TIMEOUT")
            self.assertEqual(result["blocker_kind"], "target_files_unchanged")
            self.assertTrue(_worker_result_should_retry(result))

    def test_recover_nonzero_result_with_diff_clears_blocker_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")
            subprocess_result = mock.Mock(
                returncode=0,
                stdout=" M src/existing.ts\n?? .aca/pr_candidate_context.json\n",
                stderr="",
            )

            with mock.patch(
                "src.tandem_agents.core.execution.worker.git_diff_stat",
                return_value=" M src/existing.ts",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker.run_command",
                return_value=subprocess_result,
            ):
                result = _recover_nonzero_result_with_diff(
                    {
                        "returncode": 1,
                        "stdout": "Worker exited after writing the requested file.\n",
                        "failure_reason": "worker exited after writing the requested file",
                        "blocker_kind": "worker_no_diff",
                        "recovery_action": "retry",
                    },
                    log_path,
                    worktree,
                    reason="retry produced diff",
                )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["recovered_success"])
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertNotIn("failure_reason", result)
            self.assertNotIn("blocker_kind", result)

    def test_empty_untracked_file_does_not_recover_nonzero_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            log_path = root / "worker.log"
            log_path.write_text("", encoding="utf-8")
            worktree.mkdir()
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            target = worktree / "crates" / "tandem-tools" / "tests" / "registry_path_approval.rs"
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")

            result = _recover_nonzero_result_if_diff_satisfies_subtask(
                {
                    "returncode": 1,
                    "stdout": "TOOL_MODE_REQUIRED_NOT_SATISFIED\n",
                    "failure_reason": "TOOL_MODE_REQUIRED_NOT_SATISFIED",
                    "blocker_kind": "worker_no_diff",
                },
                log_path,
                worktree,
                {
                    "title": "Add tandem-tools tests",
                    "files": ["crates/tandem-tools/tests/registry_path_approval.rs"],
                },
                require_filesystem_changes=True,
                reason="empty file",
            )

            self.assertEqual(result["returncode"], 1)
            self.assertNotIn("recovered_success", result)

            coerced = _coerce_worker_failure(
                {"returncode": 0, "stdout": "done\n"},
                log_path,
                worktree,
                {
                    "title": "Add tandem-tools tests",
                    "files": ["crates/tandem-tools/tests/registry_path_approval.rs"],
                },
                require_filesystem_changes=True,
            )
            self.assertEqual(coerced["returncode"], 1)
            self.assertEqual(coerced["failure_reason"], "NO_FILESYSTEM_CHANGES")

    def test_tool_loop_stream_blocks_without_prompt_sync_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    side_effect=[{"run_id": "run-1"}, {"run_id": "run-2"}],
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "", "completed": False, "reason": "no_text_timeout", "event_count": 251},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    return_value={"messages": [{"info": {"role": "assistant"}, "parts": [{"text": "sync fallback"}]}]},
                ) as prompt_sync:
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertEqual(result["engine"]["stream_reason"], "no_text_timeout")
            self.assertEqual(result["engine"]["retry_count"], 0)
            self.assertIsNone(result["engine"]["fallback_mode"])
            self.assertEqual(prompt_async.call_count, 1)
            prompt_sync.assert_not_called()

    def test_terminal_no_text_timeout_recovers_late_session_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-1"},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "", "completed": False, "reason": "no_text_timeout", "event_count": 22},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[
                        {"info": {"role": "assistant"}, "parts": [{"text": "late completion"}]}
                    ],
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                    prompt_sync_first=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("late completion", result["stdout"])
            self.assertEqual(result["engine"]["stream_reason"], "no_text_timeout")
            self.assertEqual(result["engine"]["messages_path"], str(Path(tmp) / "worker.engine-messages-session-1.json"))
            self.assertEqual(result["failure_reason"], "")
            self.assertEqual(result["blocker_kind"], "")

    def test_write_required_worker_can_opt_out_of_prompt_sync_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-1"},
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "async worker done", "completed": True},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.prompt_tandem_session_sync") as prompt_sync:
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "false"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("async worker done", result["stdout"])
            self.assertIsNone(result["engine"]["fallback_mode"])
            prompt_async.assert_called_once()
            prompt_sync.assert_not_called()

    def test_write_required_worker_can_override_prompt_sync_first_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-1"},
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "async worker done", "completed": True},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.prompt_tandem_session_sync") as prompt_sync:
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                    prompt_sync_first=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("async worker done", result["stdout"])
            self.assertIsNone(result["engine"]["fallback_mode"])
            prompt_async.assert_called_once()
            prompt_sync.assert_not_called()

    def test_write_required_worker_uses_prompt_sync_first_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async") as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    return_value={"messages": [{"info": {"role": "assistant"}, "parts": [{"text": "sync worker done"}]}]},
                ) as prompt_sync:
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("sync worker done", result["stdout"])
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync_first")
            prompt_async.assert_not_called()
            prompt_sync.assert_called_once()

    def test_prompt_sync_connection_failure_recovers_session_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            connect_error = RuntimeError(
                "Engine request failed for /session/session-1/prompt_sync: "
                "could not connect to http://127.0.0.1:39731 - is the engine running?"
            )
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async") as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=connect_error,
                ) as prompt_sync, \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[{"info": {"role": "assistant"}, "parts": [{"text": "sync worker recovered"}]}],
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_health",
                    return_value={"ready": True, "healthy": True},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker._engine_sync_conflict_wait_seconds",
                    return_value=1.0,
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("sync worker recovered", result["stdout"])
            self.assertEqual(prompt_sync.call_count, 3)
            self.assertEqual(result["engine"]["retry_count"], 2)
            prompt_async.assert_not_called()

    def test_prompt_sync_connection_failure_blocks_without_recovered_session_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            connect_error = RuntimeError(
                "Engine request failed for /session/session-1/prompt_sync: "
                "could not connect to http://127.0.0.1:39731 - is the engine running?"
            )
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=connect_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_session_messages", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.engine_health",
                    return_value={"ready": True, "healthy": True},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker._engine_sync_conflict_wait_seconds",
                    return_value=0.01,
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_WORKSPACE_UNREACHABLE")
            self.assertEqual(result["blocker_kind"], "engine_workspace_unreachable")

    def test_manager_planning_disables_engine_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "manager.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1") as create_session, \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-1"},
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "{\"summary\":\"ok\",\"subtasks\":[],\"risks\":[],\"tests\":[]}", "completed": True},
                ) as stream_text:
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="manager",
                    prompt="plan only",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertEqual(prompt_async.call_args.kwargs["tool_mode"], "none")
            self.assertEqual(prompt_async.call_args.kwargs["tool_allowlist"], [])
            self.assertIsNone(create_session.call_args.kwargs["permission_rules"])
            stop_when_text = stream_text.call_args.kwargs["stop_when_text"]
            self.assertTrue(callable(stop_when_text))
            self.assertTrue(stop_when_text('{"summary":"ok","subtasks":[],"risks":[],"tests":[]}'))

    def test_empty_async_stream_recovers_text_from_run_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async", return_value={"run_id": "run-1"}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_stream_run_text", return_value={"text": "", "completed": True}), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_run_events",
                    return_value=[{"type": "message.part.updated", "properties": {"delta": {"text": "event text"}}}],
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_session_messages", return_value=[]):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("event text", result["stdout"])
            self.assertTrue((Path(tmp) / "worker.engine-events-run-1.json").exists())

    def test_empty_async_stream_recovers_text_from_session_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async", return_value={"run_id": "run-1"}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_stream_run_text", return_value={"text": "", "completed": True}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[{"info": {"role": "assistant"}, "parts": [{"text": "message text"}]}],
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("message text", result["stdout"])
            self.assertTrue((Path(tmp) / "worker.engine-messages-session-1.json").exists())

    def test_run_event_404_is_recorded_as_recovery_note(self) -> None:
        class Response:
            status_code = 404

        class NotFound(Exception):
            response = Response()

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async", return_value={"run_id": "run-1"}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_stream_run_text", return_value={"text": "", "completed": True}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", side_effect=NotFound()), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[{"info": {"role": "assistant"}, "parts": [{"text": "message text"}]}],
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("message text", result["stdout"])
            self.assertEqual(result["engine"]["recovery"][0]["errors"], [])
            self.assertIn("engine run events were unavailable", result["engine"]["recovery"][0]["notes"][0])

    def test_empty_async_stream_retries_then_uses_prompt_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    side_effect=[{"run_id": "run-1"}, {"run_id": "run-2"}],
                ) as prompt_async, \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_stream_run_text", return_value={"text": "", "completed": True}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_session_messages", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    return_value={"messages": [{"info": {"role": "assistant"}, "parts": [{"text": "sync fallback"}]}]},
                ):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("sync fallback", result["stdout"])
            self.assertEqual(result["engine"]["retry_count"], 1)
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync")
            self.assertEqual(prompt_async.call_count, 2)

    def test_double_empty_engine_response_blocks_with_specific_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    side_effect=[{"run_id": "run-1"}, {"run_id": "run-2"}],
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_stream_run_text", return_value={"text": "", "completed": True}), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_session_messages", return_value=[]), \
                mock.patch("src.tandem_agents.core.execution.worker.prompt_tandem_session_sync", return_value={"messages": []}):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=False,
                    write_required=False,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_EMPTY_RESPONSE")
            self.assertEqual(result["blocker_kind"], "engine_empty_response")

    def test_prompt_sync_session_conflict_blocks_with_specific_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            conflict_error = RuntimeError(
                'Engine request failed (409) for /session/session-1/prompt_sync: '
                '{"activeRun":{"runID":"run-active"},"code":"SESSION_RUN_CONFLICT","retryAfterMs":500}'
            )
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    side_effect=[{"run_id": "run-1"}, {"run_id": "run-2"}],
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "", "completed": False, "reason": "max_events_without_text", "event_count": 151},
                ), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_session_messages", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=conflict_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_sync_conflict_wait_seconds", return_value=0.01):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_SESSION_RUN_CONFLICT")
            self.assertEqual(result["blocker_kind"], "engine_session_run_conflict")
            self.assertEqual(result["engine"]["sync_conflict"]["run_id"], "run-active")

    def test_prompt_sync_session_conflict_recovers_active_run_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            conflict_error = RuntimeError(
                'Engine request failed (409) for /session/session-1/prompt_sync: '
                '{"activeRun":{"runID":"run-active"},"code":"SESSION_RUN_CONFLICT","retryAfterMs":1}'
            )
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async") as prompt_async, \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[{"info": {"role": "assistant"}, "parts": [{"text": "recovered sync worker"}]}],
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=conflict_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_sync_conflict_wait_seconds", return_value=1.0):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("recovered sync worker", result["stdout"])
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync_first")
            self.assertEqual(result["engine"]["sync_conflict"]["run_id"], "run-active")
            prompt_async.assert_not_called()

    def test_prompt_sync_session_conflict_with_tool_activity_blocks_as_tool_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            conflict_error = RuntimeError(
                'Engine request failed (409) for /session/session-1/prompt_sync: '
                '{"activeRun":{"runID":"run-active"},"code":"SESSION_RUN_CONFLICT","retryAfterMs":1}'
            )
            with mock.patch("src.tandem_agents.core.execution.worker.create_tandem_session", return_value="session-1"), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async") as prompt_async, \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_run_events", return_value=[]), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_session_messages",
                    return_value=[
                        {
                            "info": {"role": "user"},
                            "parts": [
                                {"text": "do work", "type": "text"},
                                {"tool": "write", "type": "tool", "result": "ok"},
                            ],
                        }
                    ],
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=conflict_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_sync_conflict_wait_seconds", return_value=0.01):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={"ACA_WORKER_PROMPT_SYNC_FIRST": "true"}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync_first")
            prompt_async.assert_not_called()

    def test_prompt_sync_timeout_recovers_with_async_worker_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            timeout_error = RuntimeError(
                "Engine request failed for /session/session-1/prompt_sync: operation timed out"
            )
            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                side_effect=["session-1", "session-2"],
            ), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-async"},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "async recovered worker", "completed": True},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=timeout_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_prompt_sync_timeout_seconds", return_value=5.0):
                result = stream_tandem_prompt(
                    SimpleNamespace(),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 0)
            self.assertIn("async recovered worker", result["stdout"])
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync_first_async_recovery")
            self.assertEqual(result["session_id"], "session-2")
            self.assertEqual(result["engine"].get("prompt_sync_first_session_id"), "session-1")

    def test_prompt_sync_first_hard_timeout_blocks_if_engine_call_hangs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"

            def hang_foreverish(*_args: object, **_kwargs: object) -> dict[str, object]:
                time.sleep(2.0)
                return {}

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                side_effect=["session-1", "session-2"],
            ), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    return_value={"run_id": "run-async"},
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                    return_value={"text": "", "completed": False, "reason": "timeout"},
                ), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=hang_foreverish,
                ) as prompt_sync, \
                mock.patch("src.tandem_agents.core.execution.worker._engine_prompt_sync_timeout_seconds", return_value=0.1):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_PROMPT_TIMEOUT")
            self.assertEqual(result["blocker_kind"], "engine_prompt_timeout")
            self.assertEqual(result["engine"]["fallback_mode"], "prompt_sync_first_async_recovery")
            self.assertIn("terminal response", result["stdout"])
            prompt_async.assert_called_once()
            prompt_sync.assert_called_once()

    def test_async_recovery_dispatch_timeout_returns_prompt_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.log"
            timeout_error = RuntimeError(
                "Engine request failed for /session/session-1/prompt_sync: operation timed out"
            )

            def hang_dispatch(*_args: object, **_kwargs: object) -> dict[str, object]:
                time.sleep(1.0)
                return {"run_id": "run-too-late"}

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                side_effect=["session-1", "session-2"],
            ), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async",
                    side_effect=hang_dispatch,
                ) as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.sdk_stream_run_text",
                ) as stream_text, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=timeout_error,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_prompt_sync_timeout_seconds", return_value=0.1), \
                mock.patch("src.tandem_agents.core.execution.worker._engine_async_dispatch_timeout_seconds", return_value=0.1):
                started = time.monotonic()
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=Path(tmp),
                    provider="openai",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )
                elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.8)
            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_PROMPT_TIMEOUT")
            self.assertEqual(result["blocker_kind"], "engine_prompt_timeout")
            self.assertEqual(result["engine"]["stream_reason"], "dispatch_timeout")
            self.assertIn("async prompt dispatch", result["stdout"])
            prompt_async.assert_called_once()
            stream_text.assert_not_called()

    def test_prompt_sync_timeout_with_partial_diff_does_not_overlap_async_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "aca@example.test"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "ACA"], cwd=worktree, check=True)
            (worktree / "src").mkdir()
            (worktree / "src/lib.rs").write_text("pub fn existing() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/lib.rs"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            log_path = worktree / "worker.log"

            def hang_after_partial_diff(*_args: object, **_kwargs: object) -> dict[str, object]:
                (worktree / "src/lib.rs").write_text(
                    "pub fn existing() {}\npub fn added_by_worker() {}\n",
                    encoding="utf-8",
                )
                time.sleep(2.0)
                return {}

            with mock.patch(
                "src.tandem_agents.core.execution.worker.create_tandem_session",
                return_value="session-1",
            ), \
                mock.patch("src.tandem_agents.core.execution.worker.delete_tandem_session"), \
                mock.patch("src.tandem_agents.core.execution.worker.sdk_sessions_prompt_async") as prompt_async, \
                mock.patch(
                    "src.tandem_agents.core.execution.worker.prompt_tandem_session_sync",
                    side_effect=hang_after_partial_diff,
                ), \
                mock.patch("src.tandem_agents.core.execution.worker._worker_prompt_sync_timeout_seconds", return_value=0.1):
                result = stream_tandem_prompt(
                    SimpleNamespace(env={}),
                    role="worker-1",
                    prompt="do work",
                    cwd=worktree,
                    provider="openai-codex",
                    model="gpt-5.5",
                    env={},
                    log_path=log_path,
                    require_tool_use=True,
                    write_required=True,
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_PROMPT_TIMEOUT")
            self.assertEqual(result["blocker_kind"], "engine_prompt_timeout")
            self.assertTrue(result["engine"].get("partial_diff_recovery_deferred"))
            prompt_async.assert_not_called()

    def test_github_tasks_do_not_treat_readable_targets_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 1, "stdout": "no changes"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
                require_filesystem_changes=True,
            )

            self.assertEqual(result["returncode"], 1)
            self.assertNotEqual(result.get("verified_existing"), True)

    def test_local_tasks_can_still_verify_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 1, "stdout": "no changes"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
            )

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result.get("verified_existing"))

    def test_engine_tool_loop_with_readable_targets_stays_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "ENGINE_TOOL_LOOP_STALLED\n",
                    "failure_reason": "ENGINE_TOOL_LOOP_STALLED",
                    "blocker_kind": "engine_tool_loop_stalled",
                },
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED_NO_DIFF")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled_no_diff")
            self.assertNotEqual(result.get("verified_existing"), True)

    def test_engine_exception_with_readable_targets_stays_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {
                    "returncode": 1,
                    "stdout": "Error: Engine request failed (409) for /session/session-1/prompt_sync\n",
                    "failure_reason": "ENGINE_EXCEPTION",
                    "blocker_kind": "engine_exception",
                },
                log_path,
                worktree,
                {"files": ["src/lib.rs"]},
            )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["failure_reason"], "ENGINE_EXCEPTION")
            self.assertEqual(result["blocker_kind"], "engine_exception")
            self.assertNotEqual(result.get("verified_existing"), True)

    def test_pr_candidate_subtask_requires_real_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            target = worktree / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")

            result = _coerce_worker_failure(
                {"returncode": 1, "stdout": "no changes"},
                log_path,
                worktree,
                {"files": ["src/lib.rs"], "pr_candidate_context": [{"number": 1459}]},
            )

            self.assertEqual(result["returncode"], 1)
            self.assertNotEqual(result.get("verified_existing"), True)

    def test_github_tasks_do_not_tolerate_no_change_worker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text(
                "tenant principal explicit hosted automation workspace actor constructor\n"
                "authority request local implicit defaults organization context\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                task={"source": {"type": "github_project"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Add explicit tenant principal constructors",
                        "goal": "Add explicit tenant principal constructors for hosted automation",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[{"subtask_id": "subtask-1", "status": "failed"}],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_pr_candidate_tasks_do_not_tolerate_no_change_worker_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"raw_issue_body": "Inspect candidate PR #1459"},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Apply PR candidate",
                        "goal": "Apply still relevant changes",
                        "files": ["src/lib.rs"],
                        "pr_candidate_context": [{"number": 1459}],
                    }
                ],
                worker_results=[{"subtask_id": "subtask-1", "status": "failed"}],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_corrupt_worker_diff_is_not_tolerated_when_targets_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"source": {"type": "linear"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Edit existing source",
                        "goal": "Make a real source change",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[
                    {
                        "subtask_id": "subtask-1",
                        "worker_id": "worker-1",
                        "status": "failed",
                        "blocker_kind": "worker_corrupt_diff",
                        "failure_reason": "WORKER_CORRUPT_DIFF",
                    }
                ],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_runaway_worker_diff_is_not_tolerated_when_targets_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"source": {"type": "linear"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Edit existing source",
                        "goal": "Make a real source change",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[
                    {
                        "subtask_id": "subtask-1",
                        "worker_id": "worker-1",
                        "status": "failed",
                        "blocker_kind": "worker_runaway_diff",
                        "failure_reason": "WORKER_RUNAWAY_DIFF",
                    }
                ],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_no_diff_worker_failure_is_not_tolerated_when_targets_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"source": {"type": "linear"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Edit existing source",
                        "goal": "Make a real source change",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[
                    {
                        "subtask_id": "subtask-1",
                        "worker_id": "worker-1",
                        "status": "failed",
                        "blocker_kind": "worker_no_diff",
                        "failure_reason": "NO_FILESYSTEM_CHANGES",
                    }
                ],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_incomplete_diff_worker_blocker_is_not_tolerated_when_targets_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"source": {"type": "linear"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Edit existing source",
                        "goal": "Make a real source change",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[
                    {
                        "subtask_id": "subtask-1",
                        "worker_id": "worker-1",
                        "status": "failed",
                        "blocker_kind": "worker_incomplete_diff",
                        "failure_reason": "WORKER_REPORTED_BLOCKER",
                    }
                ],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")

    def test_reported_worker_blocker_is_not_tolerated_when_targets_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src/lib.rs"
            target.parent.mkdir(parents=True)
            target.write_text("pub fn existing() {}\n", encoding="utf-8")
            ctx = SimpleNamespace(
                task={"source": {"type": "linear"}},
                repo_path=repo_path,
                planned_subtasks=[
                    {
                        "id": "subtask-1",
                        "title": "Edit existing source",
                        "goal": "Make a real source change",
                        "files": ["src/lib.rs"],
                    }
                ],
                worker_results=[
                    {
                        "subtask_id": "subtask-1",
                        "worker_id": "worker-1",
                        "status": "failed",
                        "blocker_kind": "worker_reported_blocker",
                        "failure_reason": "WORKER_REPORTED_BLOCKER",
                    }
                ],
                blackboard={"subtasks": [{"id": "subtask-1", "status": "failed"}]},
            )

            _apply_tolerated_failures(ctx)

            self.assertEqual(ctx.worker_results[0]["status"], "failed")
            self.assertNotEqual(ctx.blackboard["subtasks"][0]["status"], "tolerated_failure")


if __name__ == "__main__":
    unittest.main()
