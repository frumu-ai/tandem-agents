from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.tandem_agents.core.execution.worker import (
    _coerce_worker_failure,
    _extract_prompt_sync_text,
    _extract_session_reply,
    _materialize_worker_context,
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

    def test_run_worker_subtask_syncs_successful_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            run_dir = root / "run"
            worktree = root / "worktree"
            repo_path.mkdir()
            worktree.mkdir()
            cfg = SimpleNamespace(provider_for_role=lambda role: ("openai-codex", "gpt-5.5"))
            result = {
                "returncode": 0,
                "stdout": "changed files",
                "log_path": str(run_dir / "logs" / "worker-1.log"),
            }

            with mock.patch("src.tandem_agents.core.execution.worker.create_worktree", return_value=worktree), \
                mock.patch("src.tandem_agents.core.execution.worker._worktree_preflight", return_value=(True, "ok")), \
                mock.patch("src.tandem_agents.core.execution.worker.effective_tandem_provider", return_value="openai-codex"), \
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
            self.assertIn("API key", result["failure_reason"])

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


if __name__ == "__main__":
    unittest.main()
