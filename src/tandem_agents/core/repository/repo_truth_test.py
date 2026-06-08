from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.core.repository.repo_truth import (
    discover_repo_files,
    infer_command_checks,
    repo_context_summary,
    run_command_checks,
    subtask_satisfied,
)


class RepoTruthDiscoveryTest(unittest.TestCase):
    def test_discovers_typescript_source_files_for_todo_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "App.tsx").write_text("export function App() { return null; }\n", encoding="utf-8")
            (repo_path / "src" / "hooks").mkdir(parents=True, exist_ok=True)
            (repo_path / "src" / "hooks" / "useTodos.ts").write_text(
                "export const useTodos = () => ({ todos: [] });\n",
                encoding="utf-8",
            )
            (repo_path / "README.md").write_text("TODO app with due dates and filters.\n", encoding="utf-8")

            task = {
                "title": "Add due dates + overdue highlighting + filters to the TODO app",
                "description": "Add due date support for each todo item, show overdue visual state, and provide list filters.",
                "acceptance_criteria": [
                    "Users can set an optional due date when creating a todo.",
                    "Filter controls (All, Active, Completed, Overdue) work correctly.",
                ],
            }

            discovered = discover_repo_files(repo_path, task, limit=5)
            self.assertIn("src/App.tsx", discovered)
            self.assertIn("src/hooks/useTodos.ts", discovered)

            summary = repo_context_summary(repo_path, task, limit=5)
            self.assertIn("Likely relevant repo files:", summary)
            self.assertIn("src/App.tsx", summary)

    def test_discovers_and_verifies_plain_html_todo_apps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "index.html").write_text(
                """
                <!DOCTYPE html>
                <html>
                  <body>
                    <input id="todo-input" />
                    <input id="due-date-input" type="date" />
                    <button class="filter-btn" data-filter="all">All</button>
                    <script>
                      const localStorageKey = "todo-app";
                      function isOverdue(todo) { return false; }
                      function isDueToday(todo) { return false; }
                    </script>
                  </body>
                </html>
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            (repo_path / "styles.css").write_text(
                ".todo-item { color: #000; }\n.todo-item.overdue { color: #c00; }\n",
                encoding="utf-8",
            )
            (repo_path / "package.json").write_text("{\"name\":\"hello-tandem\"}\n", encoding="utf-8")
            (repo_path / "README.md").write_text("Todo app with due dates, filters, and persistence.\n", encoding="utf-8")

            task = {
                "title": "cleanup",
                "description": "Add due dates + overdue highlighting + filters to the TODO app",
                "acceptance_criteria": [
                    "Users can set an optional due date when creating a todo.",
                    "Users can edit/remove due date on existing todos.",
                    "Filter controls (All, Active, Completed, Overdue) work correctly.",
                ],
            }

            discovered = discover_repo_files(repo_path, task, limit=6)
            self.assertIn("index.html", discovered)
            self.assertIn("styles.css", discovered)

            subtask = {
                "title": "cleanup - slice 1",
                "goal": "Add due date support, overdue highlighting, and filters to the todo app.",
                "acceptance_criteria": task["acceptance_criteria"],
                "files": ["index.html", "styles.css"],
            }
            self.assertTrue(subtask_satisfied(repo_path, subtask))

    def test_discovers_repo_files_without_ripgrep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "index.html").write_text(
                "<html><body><input id='due-date-input' /><button class='filter-btn'>All</button></body></html>\n",
                encoding="utf-8",
            )
            (repo_path / "styles.css").write_text(".todo-item.overdue { color: red; }\n", encoding="utf-8")
            (repo_path / "README.md").write_text("Todo app with due dates and filters.\n", encoding="utf-8")

            task = {
                "title": "cleanup",
                "description": "Add due dates + overdue highlighting + filters to the TODO app",
                "acceptance_criteria": [
                    "Users can set an optional due date when creating a todo.",
                    "Filter controls (All, Active, Completed, Overdue) work correctly.",
                ],
            }

            with patch("src.tandem_agents.core.repository.repo_truth.subprocess.run", side_effect=FileNotFoundError):
                discovered = discover_repo_files(repo_path, task, limit=5)

            self.assertIn("index.html", discovered)
            self.assertIn("styles.css", discovered)

    def test_infers_package_build_and_smoke_verification_for_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            package_dir = repo_path / "packages" / "tandem-control-panel"
            (package_dir / "src").mkdir(parents=True)
            (package_dir / "package.json").write_text(
                """
                {
                  "scripts": {
                    "build": "vite build",
                    "test:smoke": "node --test tests/smoke.test.mjs"
                  }
                }
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            (package_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            commands = infer_command_checks(
                repo_path,
                ["packages/tandem-control-panel/src/App.tsx"],
                task={"acceptance_criteria": ["Run frontend lint/typecheck and relevant tests."]},
            )

            self.assertEqual(
                commands,
                [
                    "pnpm -C packages/tandem-control-panel run build",
                    "pnpm -C packages/tandem-control-panel run test:smoke",
                ],
            )

    def test_infer_command_checks_ignores_files_without_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

            self.assertEqual(infer_command_checks(repo_path, ["src/app.py"], task={}), [])

    def test_run_command_checks_preserves_npm_global_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            completed = subprocess.CompletedProcess(
                args=["/bin/bash", "-c", "pnpm --version"],
                returncode=0,
                stdout="10.23.0\n",
                stderr="",
            )

            with patch("src.tandem_agents.core.repository.repo_truth.subprocess.run", return_value=completed) as run_mock:
                results = run_command_checks(repo_path, ["pnpm --version"])

            self.assertEqual(results[0]["status"], "pass")
            args, kwargs = run_mock.call_args
            self.assertEqual(args[0], ["/bin/bash", "-c", "pnpm --version"])
            path_parts = kwargs["env"]["PATH"].split(os.pathsep)
            self.assertIn("/home/node/npm/bin", path_parts)
            self.assertNotIn("-lc", args[0])


if __name__ == "__main__":
    unittest.main()
