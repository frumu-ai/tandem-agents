from __future__ import annotations

import tempfile
import unittest
import time
from pathlib import Path

from textwrap import dedent

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.engine.prompts import build_manager_prompt
from src.tandem_agents.core.execution.runner_core import (
    _all_subtasks_verified_existing,
    _execute_local_worker_pool,
    _prepare_subtasks_with_discovery,
    _record_worker_result,
    _record_coding_run_contract,
    _record_review_policy,
)


class RunnerCoreDiscoveryTest(unittest.TestCase):
    def test_empty_manager_plan_still_injects_discovered_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "index.html").write_text("<html><body>Todo app</body></html>\n", encoding="utf-8")
            (repo_path / "styles.css").write_text(".todo-item { color: #000; }\n", encoding="utf-8")
            task = {
                "title": "cleanup",
                "description": "Add due dates + overdue highlighting + filters to the TODO app",
                "acceptance_criteria": [
                    "Users can set an optional due date when creating a todo.",
                    "Filter controls (All, Active, Completed, Overdue) work correctly.",
                ],
            }

            discovered_files, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertIn("index.html", discovered_files)
            self.assertIn("styles.css", discovered_files)
            self.assertTrue(subtasks)
            self.assertTrue(subtasks[0]["files"])

    def test_verified_existing_short_circuit_requires_all_subtasks_satisfied(self) -> None:
        subtasks = [
            {"id": "subtask-1", "files": ["index.html", "styles.css"]},
            {"id": "subtask-2", "files": ["package.json"]},
        ]
        worker_results = [
            {"subtask_id": "subtask-1", "status": "skipped_existing"},
            {"subtask_id": "subtask-2", "status": "tolerated_failure"},
        ]

        self.assertTrue(_all_subtasks_verified_existing(subtasks, worker_results, {"ok": True}))
        self.assertFalse(_all_subtasks_verified_existing(subtasks, worker_results[:1], {"ok": True}))

    def test_verified_existing_short_circuit_rejects_github_project_drafts(self) -> None:
        subtasks = [{"id": "subtask-1", "files": ["index.html"]}]
        worker_results = [{"subtask_id": "subtask-1", "status": "skipped_existing"}]

        self.assertFalse(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {"source": {"type": "github_project", "project_item_id": 123}},
            )
        )
        self.assertTrue(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {
                    "source": {
                        "type": "github_project",
                        "project_item_id": 123,
                        "issue_url": "https://github.com/frumu-ai/tandem/issues/1",
                    }
                },
            )
        )

    def test_worker_results_are_deduplicated_by_subtask(self) -> None:
        worker_results: list[dict[str, object]] = []
        blackboard = {"workers": []}
        first = {
            "worker_id": "worker-1",
            "subtask_id": "subtask-1",
            "title": "cleanup - slice 1",
            "status": "failed",
        }
        second = {
            "worker_id": "worker-1",
            "subtask_id": "subtask-1",
            "title": "cleanup - slice 1",
            "status": "completed",
        }

        _record_worker_result(blackboard, worker_results, first)
        _record_worker_result(blackboard, worker_results, second)

        self.assertEqual(len(worker_results), 1)
        self.assertEqual(len(blackboard["workers"]), 1)
        self.assertEqual(worker_results[0]["status"], "completed")
        self.assertEqual(blackboard["workers"][0]["status"], "completed")

    def test_manager_prompt_includes_previous_feedback_for_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Repair flow
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            task = {"title": "Repair flow", "description": "Fix verification failures"}
            repo = {"path": "/tmp/repo"}
            prompt = build_manager_prompt(
                "run-1",
                task,
                repo,
                cfg,
                repo_context="src/app.py",
                previous_feedback="Reviewer Feedback:\nplease fix the tests",
            )

            self.assertIn("Reviewer Feedback:", prompt)
            self.assertIn("please fix the tests", prompt)

    def test_runner_records_coding_run_contract_in_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blackboard: dict[str, object] = {}
            contract = build_coding_run_contract(
                run_id="run-3",
                task={"title": "Fix README", "source": {"type": "github_project"}},
                repo_path=root,
                branch_name="aca/example/fix-readme-run-3",
                expected_repo_files=["README.md"],
            )

            _record_coding_run_contract(blackboard, contract)
            _record_coding_run_contract(blackboard, contract)

            self.assertIn("coding_run_contract", blackboard)
            self.assertEqual(blackboard["coding_run_contract"]["handoff_mode"], "code_edit")
            self.assertIn("Coding run contract: diff review and minimal verification are required before handoff.", blackboard["notes"])
            self.assertEqual(
                blackboard["notes"].count(
                    "Coding run contract: diff review and minimal verification are required before handoff."
                ),
                1,
            )

    def test_runner_records_review_policy_in_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Review policy
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    review:
                      policy: human_review
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            blackboard: dict[str, object] = {}

            _record_review_policy(blackboard, cfg)

            self.assertIn("review_policy", blackboard)
            self.assertTrue(blackboard["review_policy"]["human_review_required"])
            self.assertIn("human review gate required before merge.", blackboard["notes"][0].lower())

    def test_local_worker_pool_returns_completed_results_in_completion_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: Parallel work
                    repository:
                      slug: frumu-ai/example
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = resolve_config(root)
            repo_path = root / "repo"
            run_dir = root / "runs" / "run-1"
            repo_path.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            task = {"title": "Parallel work", "description": "exercise the worker pool"}
            pending_subtasks = [
                {"id": "subtask-1", "title": "slow", "goal": "slow", "write_required": True},
                {"id": "subtask-2", "title": "fast", "goal": "fast", "write_required": True},
            ]
            call_order: list[str] = []

            def fake_worker_runner(
                _cfg,
                _run_id,
                _repo_path,
                _run_dir,
                _task,
                subtask,
                worker_id,
                index,
            ):
                call_order.append(worker_id)
                if worker_id == "worker-1":
                    time.sleep(0.15)
                return {
                    "worker_id": worker_id,
                    "subtask_index": index,
                    "subtask_id": subtask["id"],
                    "title": subtask["title"],
                    "status": "completed",
                    "returncode": 0,
                    "worktree": str(repo_path),
                    "log_path": "",
                    "output_excerpt": worker_id,
                    "write_required": True,
                    "verified_existing": False,
                }

            results = _execute_local_worker_pool(
                cfg,
                "run-1",
                repo_path,
                run_dir,
                task,
                pending_subtasks,
                2,
                worker_runner=fake_worker_runner,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["worker_id"], "worker-2")
            self.assertCountEqual(call_order, ["worker-1", "worker-2"])


if __name__ == "__main__":
    unittest.main()
