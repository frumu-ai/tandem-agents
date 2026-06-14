from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.repository.repo_context import repo_context_for_task


class RepoContextForTaskTest(unittest.TestCase):
    def test_uses_repo_context_bundle_and_writes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifacts" / "repo_context_bundle.json"
            task = {
                "task_id": "TAN-143",
                "title": "Replace ACA repo_context_summary",
                "description": "Use repo.context_bundle during planning.",
                "task_contract": {"target_files": ["src/tandem_agents/core/phases/planning.py"]},
            }
            tool_result = {
                "output": json.dumps(
                    {
                        "suggested_first_reads": ["src/tandem_agents/core/phases/planning.py"],
                        "likely_files": [
                            {
                                "file_path": "src/tandem_agents/core/phases/planning.py",
                                "reason": "matches planning",
                                "confidence": "EXTRACTED",
                            }
                        ],
                        "relevant_symbols": [
                            {
                                "symbol": "run_manager_prompt",
                                "file_path": "src/tandem_agents/core/phases/planning.py",
                                "kind": "function",
                                "confidence": "EXTRACTED",
                            }
                        ],
                        "graph_edges": [],
                        "test_targets": ["src/tandem_agents/core/phases/planning_test.py"],
                        "gaps": [],
                    }
                ),
                "metadata": {
                    "tool": "repo.context_bundle",
                    "index_source": "stored",
                    "structured": {
                        "suggested_first_reads": ["src/tandem_agents/core/phases/planning.py"],
                        "likely_files": [
                            {
                                "file_path": "src/tandem_agents/core/phases/planning.py",
                                "reason": "matches planning",
                                "confidence": "EXTRACTED",
                            }
                        ],
                        "relevant_symbols": [
                            {
                                "symbol": "run_manager_prompt",
                                "file_path": "src/tandem_agents/core/phases/planning.py",
                                "kind": "function",
                                "confidence": "EXTRACTED",
                            }
                        ],
                        "graph_edges": [],
                        "test_targets": ["src/tandem_agents/core/phases/planning_test.py"],
                        "gaps": [],
                    },
                },
            }

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                return_value=tool_result,
            ) as execute:
                result = repo_context_for_task(SimpleNamespace(), root, task, artifact_path=artifact)

            self.assertEqual(result.source, "repo.context_bundle")
            self.assertFalse(result.fallback_used)
            self.assertEqual(result.path_scope, "src/tandem_agents/core/phases")
            self.assertEqual(result.required_files, ["src/tandem_agents/core/phases/planning.py"])
            self.assertEqual(result.index_source, "stored")
            self.assertIn("Repo intelligence context bundle", result.text)
            self.assertIn("run_manager_prompt", result.text)
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[1], "repo.context_bundle")
            self.assertEqual(
                execute.call_args.args[2]["required_files"],
                ["src/tandem_agents/core/phases/planning.py"],
            )
            self.assertEqual(execute.call_args.args[2]["repo_path"], ".")
            self.assertEqual(execute.call_args.args[2]["path_scope"], "src/tandem_agents/core/phases")
            self.assertEqual(execute.call_args.args[2]["readable_paths"], ["."])
            self.assertEqual(execute.call_args.args[2]["__workspace_root"], str(root))
            self.assertTrue(artifact.exists())
            saved = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(saved["source"], "repo.context_bundle")
            self.assertEqual(saved["index_source"], "stored")
            self.assertEqual(saved["engine_workspace_root"], str(root))
            self.assertEqual(saved["path_scope"], "src/tandem_agents/core/phases")
            self.assertEqual(saved["graph_hints"]["path_scope"], "src/tandem_agents/core/phases")
            self.assertEqual(saved["graph_hints"]["required_files"], ["src/tandem_agents/core/phases/planning.py"])

    def test_meta_harness_task_selects_eval_crate_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = {
                "task_id": "TAN-106",
                "identifier": "TAN-106",
                "title": "MH-04 Add prompt-injection eval coverage",
                "description": "Add fixtures and scoring in crates/tandem-meta-harness-eval for the meta-harness.",
                "labels": ["Meta-Harness", "evaluation"],
            }
            tool_result = {
                "output": "{}",
                "metadata": {
                    "index_source": "stored",
                    "structured": {"suggested_first_reads": [], "likely_files": []},
                },
            }

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                return_value=tool_result,
            ) as execute:
                result = repo_context_for_task(SimpleNamespace(), root, task)

            self.assertEqual(result.source, "repo.context_bundle")
            self.assertEqual(result.path_scope, "crates/tandem-meta-harness-eval")
            args = execute.call_args.args[2]
            self.assertEqual(args["path_scope"], "crates/tandem-meta-harness-eval")
            self.assertIn("TAN-106", args["task"])
            self.assertIn("Meta-Harness", args["task"])
            self.assertIn("crates/tandem-meta-harness-eval", args["task"])

    def test_free_text_path_scope_preserves_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = {
                "title": "Fix admin portal route handling",
                "description": "The broken screen lives under apps/AdminPortal/src/App.tsx.",
            }
            tool_result = {
                "output": "{}",
                "metadata": {
                    "index_source": "stored",
                    "structured": {"suggested_first_reads": [], "likely_files": []},
                },
            }

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                return_value=tool_result,
            ) as execute:
                result = repo_context_for_task(SimpleNamespace(), root, task)

            self.assertEqual(result.path_scope, "apps/AdminPortal")
            self.assertEqual(execute.call_args.args[2]["path_scope"], "apps/AdminPortal")

    def test_falls_back_when_repo_context_bundle_tool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = {"title": "Update prompts"}
            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["glob"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context.repo_context_summary",
                return_value="Likely relevant repo files:\n- src/app.py (readable, 10 bytes)",
            ):
                result = repo_context_for_task(SimpleNamespace(), root, task)

            self.assertEqual(result.source, "repo_truth")
            self.assertTrue(result.fallback_used)
            self.assertIn("repo.context_bundle tool is not available", result.text)
            self.assertIn("src/app.py", result.text)

    def test_refreshes_repo_index_when_store_path_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_result = {
                "output": "{}",
                "metadata": {
                    "index_source": "stored",
                    "structured": {"suggested_first_reads": [], "likely_files": []},
                },
            }

            def fake_execute(_cfg, tool, _args):
                if tool == "repo.index":
                    return {"output": "{}", "metadata": {}}
                return tool_result

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.index", "repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context._repo_index_path_is_ignored",
                return_value=True,
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                side_effect=fake_execute,
            ) as execute:
                result = repo_context_for_task(SimpleNamespace(), root, {"title": "Plan work"})

            self.assertEqual(result.index_status, "refreshed")
            self.assertEqual([call.args[1] for call in execute.call_args_list], ["repo.index", "repo.context_bundle"])
            self.assertEqual(execute.call_args_list[0].args[2]["repo_path"], ".")
            self.assertEqual(execute.call_args_list[0].args[2]["path_scope"], ".")
            self.assertEqual(execute.call_args_list[0].args[2]["readable_paths"], ["."])

    def test_skips_repo_index_refresh_when_store_path_is_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_result = {
                "output": "{}",
                "metadata": {
                    "index_source": "ephemeral_scan_after_load_error:not found",
                    "structured": {"suggested_first_reads": [], "likely_files": []},
                },
            }

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.index", "repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context._repo_index_path_is_ignored",
                return_value=False,
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                return_value=tool_result,
            ) as execute:
                result = repo_context_for_task(SimpleNamespace(), root, {"title": "Plan work"})

            self.assertEqual(result.index_status, "skipped_unignored_store_path")
            self.assertIn(".tandem/repo-index.json", result.index_error or "")
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[1], "repo.context_bundle")


if __name__ == "__main__":
    unittest.main()
