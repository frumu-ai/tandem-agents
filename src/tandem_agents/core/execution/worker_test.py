from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.execution.worker import (
    _call_with_timeout,
    _coerce_worker_failure,
    _engine_max_events_without_text,
    _extract_prompt_sync_text,
    _extract_session_reply,
    _manager_plan_stream_complete,
    _materialize_worker_context,
    _recover_nonzero_result_with_diff,
    _recover_seeded_pr_candidate_diff,
    _seed_pr_candidate_diff,
    _seedable_pr_candidate_specs,
    _worktree_changed_files,
    _worker_result_should_retry,
    _worker_prompt_retry_suffix,
    run_worker_subtask,
    stream_tandem_prompt,
)
from src.tandem_agents.core.phases.worker_dispatch import _apply_tolerated_failures


class WorkerFailureCoercionTest(unittest.TestCase):
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

    def test_terminal_engine_blockers_do_not_retry_worker_prompt(self) -> None:
        self.assertFalse(
            _worker_result_should_retry({"returncode": 1, "blocker_kind": "engine_tool_loop_stalled"})
        )
        self.assertFalse(_worker_result_should_retry({"returncode": 1, "blocker_kind": "engine_provider_auth"}))
        self.assertTrue(_worker_result_should_retry({"returncode": 1, "blocker_kind": "worker_no_diff"}))

    def test_worker_stream_uses_tighter_empty_event_cap(self) -> None:
        cfg = SimpleNamespace(env={})

        self.assertEqual(_engine_max_events_without_text(cfg, "worker-1"), 50)
        self.assertEqual(_engine_max_events_without_text(cfg, "manager"), 150)

    def test_empty_event_cap_can_be_configured(self) -> None:
        cfg = SimpleNamespace(env={"ACA_ENGINE_MAX_EVENTS_WITHOUT_TEXT": "12"})

        self.assertEqual(_engine_max_events_without_text(cfg, "worker-1"), 12)

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

        self.assertEqual(specs, [{"number": "1459", "ref": "refs/aca/pr-1459", "files": ["src/current.ts"]}])

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
                    "files": ["src/current.ts"],
                    "pr_candidate_refs": [{"number": 1459, "ok": True, "ref": "refs/aca/pr-1459"}],
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
                        }
                    ],
                },
                log_path,
            )

            self.assertIsNotNone(seeded)
            self.assertEqual(seeded["number"], "1459")
            self.assertEqual(_worktree_changed_files(worktree), ["src/current.ts"])
            self.assertIn("reduce", target.read_text(encoding="utf-8"))

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

    def test_nonzero_worker_with_engine_error_diff_preserves_partial_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            log_path = worktree / "worker.log"
            log_path.write_text("", encoding="utf-8")
            (worktree / "src").mkdir()
            target = worktree / "src" / "existing.ts"
            target.write_text("before\n", encoding="utf-8")
            subprocess_result = mock.Mock(returncode=0, stdout="src/existing.ts\n", stderr="")

            with mock.patch(
                "src.tandem_agents.core.execution.worker.git_diff_stat",
                return_value=" src/existing.ts | 1 +\n",
            ), mock.patch(
                "src.tandem_agents.core.execution.worker.run_command",
                side_effect=[
                    subprocess_result,
                    mock.Mock(returncode=0, stdout="diff --git a/src/existing.ts b/src/existing.ts\n", stderr=""),
                    subprocess_result,
                ],
            ):
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

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_dispatch_failed")
            self.assertNotIn("recovered_success", result)
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())

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

            self.assertEqual(result["returncode"], 0)
            self.assertTrue(result["recovered_from_engine_stall"])
            self.assertEqual(result["changed_files"], ["src/existing.ts"])
            self.assertIn("engine_tool_loop_stalled_after_diff", result["warnings"])
            self.assertNotIn("failure_reason", result)
            self.assertNotIn("blocker_kind", result)
            self.assertTrue(Path(result["partial_diff_artifact"]).exists())

    def test_worker_changed_files_ignore_aca_blocker_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            subprocess.run(["git", "init"], cwd=worktree, check=True, stdout=subprocess.DEVNULL)
            (worktree / "aca-subtask-1-blocker.md").write_text("blocked\n", encoding="utf-8")
            (worktree / ".aca").mkdir()
            (worktree / ".aca" / "pr_candidate_context.json").write_text("{}\n", encoding="utf-8")

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

    def test_tool_loop_stream_blocks_without_retry_or_sync_fallback(self) -> None:
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
                    return_value={"text": "", "completed": False, "reason": "no_text_timeout", "event_count": 251},
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
                )

            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["engine"]["stream_reason"], "no_text_timeout")
            self.assertEqual(prompt_async.call_count, 1)
            prompt_sync.assert_not_called()

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
                    SimpleNamespace(),
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
                    SimpleNamespace(),
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
                    SimpleNamespace(),
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
                    SimpleNamespace(),
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
                    SimpleNamespace(),
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
                    SimpleNamespace(),
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
            self.assertEqual(result["failure_reason"], "ENGINE_TOOL_LOOP_STALLED")
            self.assertEqual(result["blocker_kind"], "engine_tool_loop_stalled")
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


if __name__ == "__main__":
    unittest.main()
