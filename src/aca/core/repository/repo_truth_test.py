from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.aca.core.repository.repo_truth import discover_repo_files, repo_context_summary, subtask_satisfied


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

            with patch("src.aca.core.repository.repo_truth.subprocess.run", side_effect=FileNotFoundError):
                discovered = discover_repo_files(repo_path, task, limit=5)

            self.assertIn("index.html", discovered)
            self.assertIn("styles.css", discovered)


if __name__ == "__main__":
    unittest.main()
