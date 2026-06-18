from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tandem_agents.core.repository.repo_truth import (
    command_check_is_executable,
    collect_expected_repo_files,
    discover_repo_files,
    extract_command_checks,
    filter_executable_command_checks,
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

    def test_subtask_satisfied_requires_exact_contract_identifiers(self) -> None:
        subtask = {
            "title": "Add scheduler throughput config loader tests",
            "goal": "Add focused config loader coverage for exact scheduler budget and backpressure fields.",
            "acceptance_criteria": [
                "Assert config.scheduler.max_concurrent_worker_runs, "
                "config.scheduler.max_daily_model_spend_cents, "
                "config.scheduler.rate_limit_backpressure, "
                "config.scheduler.ci_backpressure, and "
                "config.scheduler.merge_queue_backpressure."
            ],
            "files": ["src/tandem_agents/config/config_loader_test.py"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "src" / "tandem_agents" / "config" / "config_loader_test.py"
            target.parent.mkdir(parents=True)
            target.write_text(
                "\n".join(
                    [
                        "def test_existing_scheduler_config_loader_coverage():",
                        "    assert 'scheduler' == 'scheduler'",
                        "    assert 'config' == 'config'",
                        "    assert 'backpressure' == 'backpressure'",
                        "    assert 'budget' == 'budget'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertFalse(subtask_satisfied(repo_path, subtask))

            target.write_text(
                target.read_text(encoding="utf-8")
                + "assert config.scheduler.max_concurrent_worker_runs == 4\n"
                + "assert config.scheduler.max_daily_model_spend_cents == 0\n"
                + "assert config.scheduler.rate_limit_backpressure is True\n"
                + "assert config.scheduler.ci_backpressure is True\n"
                + "assert config.scheduler.merge_queue_backpressure is True\n",
                encoding="utf-8",
            )

            self.assertTrue(subtask_satisfied(repo_path, subtask))

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

    def test_infer_command_checks_adds_python3_unittest_for_sibling_test_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            test_path = repo_path / "src" / "tandem_agents" / "api" / "run_isolation_test.py"
            test_path.parent.mkdir(parents=True)
            test_path.write_text("import unittest\n", encoding="utf-8")

            commands = infer_command_checks(
                repo_path,
                ["src/tandem_agents/api/run_isolation_test.py"],
                task={"acceptance_criteria": ["Run the targeted test."]},
            )

            self.assertEqual(
                commands,
                ["python3 -m unittest src.tandem_agents.api.run_isolation_test"],
            )

    def test_infer_command_checks_adds_existing_python_sibling_test_for_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            src_path = repo_path / "src" / "tandem_agents" / "api" / "run_isolation.py"
            test_path = repo_path / "src" / "tandem_agents" / "api" / "run_isolation_test.py"
            src_path.parent.mkdir(parents=True)
            src_path.write_text("def ok():\n    return True\n", encoding="utf-8")
            test_path.write_text("import unittest\n", encoding="utf-8")

            commands = infer_command_checks(
                repo_path,
                ["src/tandem_agents/api/run_isolation.py"],
                task={"acceptance_criteria": ["Verify the Python behavior."]},
            )

            self.assertEqual(
                commands,
                ["python3 -m unittest src.tandem_agents.api.run_isolation_test"],
            )

    def test_infer_command_checks_adds_cargo_check_for_changed_crate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            crate_dir = repo_path / "crates" / "tandem-meta-harness-eval"
            (crate_dir / "src").mkdir(parents=True)
            (crate_dir / "Cargo.toml").write_text(
                """
                [package]
                name = "tandem-meta-harness-eval"
                version = "0.1.0"
                edition = "2021"
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            (crate_dir / "src" / "lib.rs").write_text("pub fn ok() {}\n", encoding="utf-8")

            commands = infer_command_checks(
                repo_path,
                [
                    "Cargo.toml",
                    "crates/tandem-meta-harness-eval/Cargo.toml",
                    "crates/tandem-meta-harness-eval/src/lib.rs",
                ],
                task={"title": "Define meta-harness eval crate"},
            )

            self.assertEqual(commands, ["cargo check -p tandem-meta-harness-eval"])

    def test_extract_command_checks_accepts_safe_string_cargo_commands(self) -> None:
        commands = extract_command_checks(
            {
                "tests": [
                    "cargo check -p tandem-meta-harness-eval",
                    {"command": "cargo test -p tandem-meta-harness-eval"},
                    "curl https://example.com",
                ]
            }
        )

        self.assertEqual(
            commands,
            [
                "cargo check -p tandem-meta-harness-eval",
                "cargo test -p tandem-meta-harness-eval",
            ],
        )

    def test_collect_expected_repo_files_excludes_ignored_targets(self) -> None:
        files = collect_expected_repo_files(
            [
                {
                    "files": ["docs/internal/meta-harness/KANBAN.md", "Cargo.toml"],
                    "target_files": ["docs/internal/meta-harness/KANBAN.md"],
                    "ignored_target_files": ["docs/internal/meta-harness/KANBAN.md"],
                }
            ]
        )

        self.assertEqual(files, ["Cargo.toml"])

    def test_infer_command_checks_uses_script_that_references_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "scripts").mkdir()
            (repo_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (repo_path / "package.json").write_text(
                """
                {
                  "scripts": {
                    "bug-monitor:fixture:test": "node --test scripts/bug-monitor-external-log-intake-fixture.test.mjs",
                    "docs:check": "node scripts/check-docs.mjs"
                  }
                }
                """.strip()
                + "\n",
                encoding="utf-8",
            )

            commands = infer_command_checks(
                repo_path,
                ["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
                task={"title": "Verify Bug Monitor smoke", "acceptance_criteria": ["Run focused test coverage."]},
            )

            self.assertEqual(commands, ["pnpm -C . run bug-monitor:fixture:test"])

    def test_discover_repo_files_prioritizes_bug_monitor_backend_for_quality_gate_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            files = [
                "scripts/bug-monitor-external-log-intake-smoke.mjs",
                "packages/tandem-control-panel/src/pages/BugMonitorPage.tsx",
                "crates/tandem-server/src/bug_monitor/service.rs",
                "crates/tandem-server/src/http/bug_monitor.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part01.rs",
            ]
            for rel_path in files:
                path = repo_path / rel_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("bug monitor quality_gate draft blocked\n", encoding="utf-8")

            discovered = discover_repo_files(
                repo_path,
                {
                    "title": "SIG-01 Verify Bug Monitor end-to-end against signal quality gates",
                    "acceptance_criteria": [
                        "Minor retries and duplicate failures do not create draft work.",
                        "Blocked signals remain inspectable with quality-gate reasons.",
                    ],
                },
                limit=5,
            )

            self.assertIn("crates/tandem-server/src/bug_monitor/service.rs", discovered)
            self.assertIn("crates/tandem-server/src/http/tests/bug_monitor_parts/part01.rs", discovered)
            self.assertLess(
                discovered.index("crates/tandem-server/src/http/tests/bug_monitor_parts/part01.rs"),
                discovered.index("scripts/bug-monitor-external-log-intake-smoke.mjs"),
            )

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

    def test_run_command_checks_retries_once_before_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            failed = subprocess.CompletedProcess(
                args=["/bin/bash", "-c", "pnpm test"],
                returncode=1,
                stdout="flaky timeout\n",
                stderr="",
            )
            passed = subprocess.CompletedProcess(
                args=["/bin/bash", "-c", "pnpm test"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

            with patch(
                "src.tandem_agents.core.repository.repo_truth.subprocess.run",
                side_effect=[failed, passed],
            ) as run_mock:
                results = run_command_checks(repo_path, ["pnpm test"])

            self.assertEqual(run_mock.call_count, 2)
            self.assertEqual(results[0]["status"], "pass")
            self.assertEqual(results[0]["attempt_count"], 2)
            self.assertEqual(results[0]["attempts"][0]["status"], "fail")

    def test_filter_executable_command_checks_drops_natural_language_verification(self) -> None:
        commands = filter_executable_command_checks(
            [
                "Evals fail before containment controls and pass after gateway controls are implemented.",
                "cargo test -p tandem-server eval",
                "pnpm -C packages/tandem-control-panel run build",
                "./scripts/ci-file-size-check.sh",
            ]
        )

        self.assertEqual(
            commands,
            [
                "cargo test -p tandem-server eval",
                "pnpm -C packages/tandem-control-panel run build",
                "./scripts/ci-file-size-check.sh",
            ],
        )
        self.assertFalse(command_check_is_executable("Demo or tests for both additional vertical slices."))


if __name__ == "__main__":
    unittest.main()
