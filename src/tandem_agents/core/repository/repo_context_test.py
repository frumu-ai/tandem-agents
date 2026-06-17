from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.repository.repo_context import (
    _repo_index_path_is_ignored,
    repo_context_for_task,
    repo_context_hints_for_task,
)


class RepoContextForTaskTest(unittest.TestCase):
    def test_repo_index_store_path_is_ignored_by_tandem_dir_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / ".gitignore").write_text(".tandem/\n", encoding="utf-8")

            self.assertTrue(_repo_index_path_is_ignored(root))

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
            self.assertIn("Required edit files", result.text)
            self.assertIn("Use Required edit files as the preferred worker deliverables", result.text)
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
            self.assertFalse(saved["fallback_used"])
            self.assertIsNone(saved["fallback_reason"])

    def test_empty_context_bundle_uses_fallback_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifacts" / "repo_context_bundle.json"
            task = {
                "task_id": "TAN-57",
                "title": "CRI-02 Add GitHub Projects schema drift and divergence regression coverage",
                "description": "Tests cover schema drift and degraded write capability.",
            }
            tool_result = {
                "output": json.dumps(
                    {
                        "suggested_first_reads": [],
                        "likely_files": [],
                        "relevant_symbols": [],
                        "graph_edges": [],
                        "test_targets": [],
                        "gaps": [],
                    }
                ),
                "metadata": {"tool": "repo.context_bundle", "index_source": "stored"},
            }

            with patch(
                "src.tandem_agents.core.repository.repo_context.list_engine_tool_ids",
                return_value=["repo.context_bundle"],
            ), patch(
                "src.tandem_agents.core.repository.repo_context.repo_context_summary",
                return_value="Likely relevant repo files:\n- crates/tandem-github/src/projects.rs (readable, 123 bytes)",
            ), patch(
                "src.tandem_agents.core.repository.repo_context.execute_engine_tool",
                return_value=tool_result,
            ):
                result = repo_context_for_task(SimpleNamespace(), root, task, artifact_path=artifact)

            self.assertEqual(result.source, "repo.context_bundle")
            self.assertTrue(result.fallback_used)
            self.assertEqual(result.error, "repo.context_bundle returned no actionable repo evidence")
            self.assertIn("Repo intelligence context bundle unavailable", result.text)
            self.assertIn("crates/tandem-github/src/projects.rs", result.text)
            saved = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertTrue(saved["fallback_used"])
            self.assertEqual(saved["fallback_reason"], "repo.context_bundle returned no actionable repo evidence")

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

    def test_internal_docs_reference_does_not_narrow_code_scope(self) -> None:
        task = {
            "task_id": "TAN-57",
            "title": "CRI-02 Add GitHub Projects schema drift and divergence regression coverage",
            "description": (
                "Source: `docs/internal/TANDEM_GITHUB_PROJECTS_KANBAN.md` Phase H.\n"
                "Harden GitHub Projects intake against schema drift and remote state changes."
            ),
        }

        hints = repo_context_hints_for_task(task)

        self.assertEqual(hints["path_scope"], "crates/tandem-server/src/http")
        self.assertIn("docs/internal/TANDEM_GITHUB_PROJECTS_KANBAN.md", hints["task"])
        self.assertIn("CoderGithubProjectBinding", hints["task"])
        self.assertIn("crates/tandem-server/src/http/coder_parts/part09.rs", hints["task"])

    def test_aca_worktree_isolation_task_routes_to_lifecycle_files(self) -> None:
        task = {
            "task_id": "TAN-170",
            "identifier": "TAN-170",
            "title": "LACA-12 Add per-issue worktree and branch isolation for parallel ACA runs",
            "description": (
                "Make parallel ACA execution safe by isolating each Linear issue in its "
                "own worktree/branch and detecting cross-run conflicts before PR creation."
            ),
            "acceptance_criteria": [
                "Create one worktree and branch per claimed Linear issue.",
                "Detect overlapping file edits across active ACA runs.",
                "PR metadata links back to Linear issue and ACA run id.",
            ],
        }

        hints = repo_context_hints_for_task(task)

        self.assertEqual(hints["path_scope"], "src/tandem_agents/core")
        self.assertIn("src/tandem_agents/core/phases/task_intake.py", hints["required_files"])
        self.assertIn("src/tandem_agents/core/repository/repository.py", hints["required_files"])
        self.assertIn("src/tandem_agents/core/repository/repository_test.py", hints["required_files"])
        self.assertIn("run_task_intake", hints["task"])
        self.assertIn("checkout_run_branch", hints["task"])

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
