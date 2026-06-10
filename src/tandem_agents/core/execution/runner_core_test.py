from __future__ import annotations

import json
import tempfile
import threading
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from textwrap import dedent

from src.tandem_agents.config.config_loader import resolve_config
from src.tandem_agents.core.verification.coding_run_contract import build_coding_run_contract
from src.tandem_agents.core.engine.prompts import build_manager_prompt
from src.tandem_agents.core.execution.runner_core import (
    _all_subtasks_verified_existing,
    _annotate_pr_candidate_current_layout,
    _auto_approve_loop,
    _collect_worker_changed_files,
    _execute_local_worker_pool,
    _final_lease_release_decision,
    _has_unresolved_write_required_worker_failure,
    _integration_blocker_message,
    _integration_failure_can_defer_to_review,
    _integration_semantic_blocker_can_defer_to_review,
    _linear_comment_task_summary,
    _permission_requests_from_payload,
    _preserve_and_reset_blocked_worktree,
    _prepare_subtasks_with_discovery,
    _pr_candidate_edit_goal,
    _pr_candidate_target_files,
    _pr_candidate_unexpected_changed_files,
    _normalize_manager_subtasks,
    _task_mentions_external_pr_candidates,
    _worker_failure_blocker,
    _record_worker_result,
    _record_coding_run_contract,
    _record_review_policy,
    _sticky_expected_repo_files,
)
from src.tandem_agents.core.engine.process_utils import run_command
from src.tandem_agents.core.engine.prompts import build_worker_prompt


class RunnerCoreDiscoveryTest(unittest.TestCase):
    def _config(self, root: Path):
        (root / "agent.yaml").write_text(
            dedent(
                """
                agent:
                  name: ACA
                tandem:
                  base_url: http://127.0.0.1:39733
                task_source:
                  type: manual
                  prompt: Permission test
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
        return resolve_config(root)

    def test_permission_requests_from_payload_accepts_engine_requests_shape(self) -> None:
        payload = {
            "requests": [
                {"id": "req-1", "status": "pending", "permission": "bash"},
                {"id": "req-2", "status": "allow", "permission": "bash"},
            ],
            "rules": [],
        }

        self.assertEqual(
            _permission_requests_from_payload(payload),
            [
                {"id": "req-1", "status": "pending", "permission": "bash"},
                {"id": "req-2", "status": "allow", "permission": "bash"},
            ],
        )

    def test_manager_subtask_deliverable_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "SIG-01-A",
                        "title": "Map gate flow",
                        "goal": "Confirm existing Bug Monitor gate flow.",
                        "deliverable": "A short note identifying gate APIs and the verification command.",
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["A short note identifying gate APIs and the verification command."],
        )
        self.assertEqual(
            subtasks[0]["deliverables"],
            ["A short note identifying gate APIs and the verification command."],
        )

    def test_manager_subtask_required_work_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "sig01-e2e-quality-gate-fixture",
                        "title": "Add focused end-to-end Bug Monitor quality-gate fixture coverage",
                        "goal": "Exercise a mixed Bug Monitor fixture.",
                        "required_work": [
                            "Assert minor retries do not create draft work.",
                            "Assert blocked signals include quality-gate reasons.",
                        ],
                        "verification": ["Run the focused fixture test."],
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            [
                "Assert minor retries do not create draft work.",
                "Assert blocked signals include quality-gate reasons.",
            ],
        )
        self.assertEqual(subtasks[0]["verification_commands"], ["Run the focused fixture test."])

    def test_manager_subtask_expected_verification_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Verify Bug Monitor gates"},
                [
                    {
                        "id": "sig01-e2e-quality-gate-fixture",
                        "title": "Add/refine focused fixture coverage",
                        "goal": "Exercise Bug Monitor signal quality gates.",
                        "instructions": [
                            "Add or refine a focused fixture that covers quality-gate outcomes.",
                        ],
                        "expected_verification": [
                            "Focused Bug Monitor tests pass and cover accepted, retried, and blocked signals.",
                        ],
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["Focused Bug Monitor tests pass and cover accepted, retried, and blocked signals."],
        )

    def test_manager_subtask_scope_fills_acceptance_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subtasks = _normalize_manager_subtasks(
                {"title": "Add prompt-injection exfiltration evals"},
                [
                    {
                        "title": "Add KB-MCP bulk export scenarios",
                        "goal": "Cover prompt-injected memory export attempts.",
                        "scope": "Add YAML eval scenarios and bounded-exposure assertions for no bulk export.",
                    }
                ],
                str(Path(tmp)),
            )

        self.assertEqual(
            subtasks[0]["acceptance_criteria"],
            ["Add YAML eval scenarios and bounded-exposure assertions for no bulk export."],
        )

    def test_manager_subtask_filters_gitignored_target_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            run_command(["git", "init"], cwd=repo_path)
            (repo_path / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            subtasks = _normalize_manager_subtasks(
                {"title": "Define meta-harness eval crate"},
                [
                    {
                        "title": "Define docs and crate",
                        "goal": "Define tracked crate contracts without private docs deliverables.",
                        "files": [
                            "docs/internal/meta-harness/KANBAN.md",
                            "crates/tandem-meta-harness-eval/src/lib.rs",
                        ],
                        "acceptance_criteria": ["Tracked crate contract is defined."],
                    }
                ],
                str(repo_path),
            )

        self.assertEqual(subtasks[0]["files"], ["crates/tandem-meta-harness-eval/src/lib.rs"])
        self.assertEqual(subtasks[0]["target_files"], ["crates/tandem-meta-harness-eval/src/lib.rs"])
        self.assertEqual(subtasks[0]["ignored_target_files"], ["docs/internal/meta-harness/KANBAN.md"])

    def test_manager_subtask_drops_root_manifest_only_target_after_ignored_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            run_command(["git", "init"], cwd=repo_path)
            (repo_path / ".gitignore").write_text("docs/internal/\n", encoding="utf-8")
            subtasks = _normalize_manager_subtasks(
                {"title": "Define meta-harness eval crate"},
                [
                    {
                        "title": "Define docs and manifest metadata",
                        "goal": "Define private docs and root manifest metadata.",
                        "files": [
                            "docs/internal/meta-harness/KANBAN.md",
                            "docs/internal/meta-harness/eval-crate.md",
                            "Cargo.toml",
                        ],
                        "acceptance_criteria": ["Tracked crate contract is defined."],
                    }
                ],
                str(repo_path),
            )

        self.assertEqual(subtasks[0]["files"], [])
        self.assertEqual(subtasks[0]["target_files"], [])
        self.assertEqual(
            subtasks[0]["ignored_target_files"],
            ["docs/internal/meta-harness/KANBAN.md", "docs/internal/meta-harness/eval-crate.md"],
        )
        self.assertIn("Do not satisfy this task by placing a prose specification", subtasks[0]["scope_note"])

    def test_permission_requests_from_payload_accepts_sdk_permissions_shape(self) -> None:
        payload = {
            "permissions": [
                {"request_id": "req-1", "status": "pending", "permission": "bash"},
            ],
        }

        self.assertEqual(
            _permission_requests_from_payload(payload),
            [{"request_id": "req-1", "status": "pending", "permission": "bash"}],
        )

    def test_auto_approve_loop_replies_to_pending_engine_permissions(self) -> None:
        stop_event = threading.Event()
        replied: list[tuple[str, str]] = []

        def fake_sleep(_seconds: float) -> None:
            stop_event.set()

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core.sdk_agent_teams_list_approvals",
                return_value={"approvals": []},
            ), patch(
                "src.tandem_agents.core.execution.runner_core.list_engine_permissions",
                return_value={
                    "requests": [
                        {
                            "id": "perm-1",
                            "status": "pending",
                            "permission": "apply_patch",
                        }
                    ]
                },
            ), patch(
                "src.tandem_agents.core.execution.runner_core.reply_engine_permission",
                side_effect=lambda _cfg, request_id, reply: replied.append((request_id, reply)) or {"ok": True},
            ), patch("src.tandem_agents.core.execution.runner_core.time.sleep", side_effect=fake_sleep):
                _auto_approve_loop(cfg, stop_event)

        self.assertEqual(replied, [("perm-1", "allow")])

    def test_final_lease_release_uses_nested_blocked_result(self) -> None:
        ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={})

        release_status, release_reason = _final_lease_release_decision(
            ctx,
            layout={},
            crashed_exc=None,
            result={
                "status": {
                    "run": {"status": "blocked"},
                    "blocker": {
                        "active": True,
                        "kind": "verification_failed",
                        "detail": "smoke test failed",
                    },
                }
            },
        )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "smoke test failed")

    def test_final_lease_release_reads_persisted_blocked_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "run": {"status": "blocked"},
                        "phase": {"detail": "review did not approve"},
                        "blocker": {"active": True, "kind": "verification_failed"},
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={"status": status_path})

            release_status, release_reason = _final_lease_release_decision(
                ctx,
                layout={"status": status_path},
                crashed_exc=None,
                result=None,
            )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "review did not approve")

    def test_final_lease_release_fails_closed_on_unknown_status(self) -> None:
        ctx = SimpleNamespace(status={"run": {"status": "running"}}, layout={})

        release_status, release_reason = _final_lease_release_decision(
            ctx,
            layout={},
            crashed_exc=None,
            result=None,
        )

        self.assertEqual(release_status, "blocked")
        self.assertEqual(release_reason, "run finished without terminal status")

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

    def test_single_worker_bug_monitor_subtask_narrows_overbroad_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            paths = [
                "crates/tandem-server/src/http/tests/bug_monitor.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part01.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part02.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs",
                "crates/tandem-server/src/http/tests/bug_monitor_parts/part04.rs",
                "crates/tandem-server/src/bug_monitor/log_parser.rs",
                "crates/tandem-server/src/bug_monitor/service.rs",
            ]
            for rel_path in paths:
                target = repo_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("bug monitor quality gate draft duplicate confidence retry\n", encoding="utf-8")

            task = {
                "title": "SIG-01 Verify Bug Monitor end-to-end against signal quality gates",
                "description": "Bug Monitor should prove quality gates block noisy signals.",
                "acceptance_criteria": [
                    "Minor retries, routine progress, low-confidence speculation, and duplicate failures do not create new draft work.",
                ],
            }
            manager_plan = {
                "subtasks": [
                    {
                        "title": "Add focused Bug Monitor quality-gate regression tests",
                        "goal": "Extend the existing Bug Monitor server/control-panel test path.",
                        "files": paths[:-1],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, manager_plan, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs",
                    "crates/tandem-server/src/http/tests/bug_monitor_parts/part04.rs",
                    "crates/tandem-server/src/bug_monitor/service.rs",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
            self.assertIn("ACA narrowed", subtasks[0]["scope_note"])

    def test_pr_candidate_task_does_not_use_discovered_files_as_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "crates").mkdir()
            (repo_path / "crates" / "optimizations.rs").write_text("fn existing() {}\n", encoding="utf-8")
            task = {
                "title": "Consolidate worthwhile small Bolt optimizations into one intentional PR",
                "description": "\n".join(
                    [
                        "Initial candidates to inspect/cherry-pick if still relevant:",
                        "* #1459 - 3+/3-, 3 files",
                        "* #1449 - 9+/3-, 2 files",
                        "",
                        "Acceptance:",
                        "* Apply only improvements that still make sense in the current file layout.",
                    ]
                ),
                "acceptance_criteria": [
                    "#1459 - 3+/3-, 3 files",
                    "Apply only improvements that still make sense in the current file layout.",
                ],
                "source": {"type": "linear", "item": "TAN-111"},
            }

            discovered_files, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertTrue(_task_mentions_external_pr_candidates(task))
            self.assertIn("crates/optimizations.rs", discovered_files)
            self.assertTrue(subtasks)
            self.assertEqual(subtasks[0]["files"], [])
            self.assertEqual(subtasks[0]["target_files"], [])

    def test_manager_subtasks_are_preserved_for_serial_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            raw_subtasks = [
                {"id": f"subtask-{index}", "title": f"Subtask {index}", "goal": f"Goal {index}"}
                for index in range(1, 6)
            ]

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "cleanup", "description": "cleanup"},
                {"subtasks": raw_subtasks},
                repo_path,
                3,
            )

            self.assertEqual(
                [subtask["id"] for subtask in subtasks],
                ["subtask-1", "subtask-2", "subtask-3", "subtask-4", "subtask-5"],
            )

    def test_manager_subtasks_merge_for_single_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            manager_plan = {
                "subtasks": [
                    {
                        "id": "crate",
                        "title": "Define crate boundary",
                        "goal": "Create the eval crate boundary.",
                        "files": ["Cargo.toml", "crates/tandem-eval/src/lib.rs"],
                        "acceptance_criteria": ["Eval crate boundaries are defined."],
                        "verification_commands": ["cargo check -p tandem-eval"],
                    },
                    {
                        "id": "trace",
                        "title": "Define trace contracts",
                        "goal": "Add trace-store contracts.",
                        "files": [
                            "crates/tandem-eval/src/lib.rs",
                            "crates/tandem-eval/src/trace.rs",
                            "crates/tandem-eval/tests/trace_contract.rs",
                        ],
                        "acceptance_criteria": ["Trace store and replayable trace model are specified."],
                        "verification_commands": ["cargo test -p tandem-eval --test trace_contract"],
                    },
                    {
                        "id": "scoring",
                        "title": "Define scoring contracts",
                        "goal": "Add workflow version scoring contracts.",
                        "files": [
                            "crates/tandem-eval/src/lib.rs",
                            "crates/tandem-eval/src/scoring.rs",
                            "crates/tandem-eval/tests/scoring_contract.rs",
                        ],
                        "acceptance_criteria": ["Scored workflow/version model is specified."],
                    },
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(
                {"title": "MH-01 Define meta-harness eval crate"},
                manager_plan,
                repo_path,
                1,
            )

            self.assertEqual(len(subtasks), 1)
            self.assertEqual(subtasks[0]["id"], "subtask-1")
            self.assertEqual(
                subtasks[0]["files"],
                [
                    "Cargo.toml",
                    "crates/tandem-eval/src/lib.rs",
                    "crates/tandem-eval/src/trace.rs",
                    "crates/tandem-eval/tests/trace_contract.rs",
                    "crates/tandem-eval/src/scoring.rs",
                    "crates/tandem-eval/tests/scoring_contract.rs",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])
            self.assertEqual(
                subtasks[0]["acceptance_criteria"],
                [
                    "Eval crate boundaries are defined.",
                    "Trace store and replayable trace model are specified.",
                    "Scored workflow/version model is specified.",
                ],
            )
            self.assertIn("ACA merged multiple manager subtasks", subtasks[0]["scope_note"])
            self.assertEqual([item["id"] for item in subtasks[0]["merged_subtasks"]], ["crate", "trace", "scoring"])

    def test_expected_repo_files_are_sticky_across_retries(self) -> None:
        blackboard = {
            "repo_validation": {
                "expected_files": [
                    "crates/tandem-meta-harness-eval/src/lib.rs",
                    "crates/tandem-meta-harness-eval/src/scoring.rs",
                ]
            }
        }

        expected = _sticky_expected_repo_files(
            blackboard,
            [
                "crates/tandem-meta-harness-eval/src/lib.rs",
                "crates/tandem-meta-harness-eval/src/trace.rs",
            ],
        )

        self.assertEqual(
            expected,
            [
                "crates/tandem-meta-harness-eval/src/lib.rs",
                "crates/tandem-meta-harness-eval/src/scoring.rs",
                "crates/tandem-meta-harness-eval/src/trace.rs",
            ],
        )
        self.assertEqual(blackboard["expected_repo_files"], expected)

    def test_worker_prompt_includes_pr_candidate_context_artifact(self) -> None:
        task = {
            "title": "Consolidate worthwhile small Bolt optimizations into one intentional PR",
            "description": "Inspect #1459 before editing.",
        }
        subtask = {
            "id": "subtask-1",
            "title": "Inspect PRs",
            "goal": "Inspect candidates and apply safe changes.",
            "files": [],
            "target_files": [],
            "pr_candidate_context_artifact": "artifacts/pr_candidate_context.json",
            "pr_candidate_context": [{"number": 1459, "title": "Small cleanup", "state": "open"}],
        }

        prompt = build_worker_prompt("run-1", "worker-1", subtask, task, "/tmp/worktree")

        self.assertIn("ACA already fetched GitHub PR candidate context", prompt)
        self.assertIn("artifacts/pr_candidate_context.json", prompt)
        self.assertIn('"number": 1459', prompt)
        self.assertIn("This is an edit task, not a report-only task", prompt)
        self.assertIn("Do not stop after producing an applicability matrix", prompt)

    def test_pr_candidate_target_files_are_derived_from_context_without_noise_docs(self) -> None:
        contexts = [
            {
                "number": 1459,
                "changed_files": [
                    ".jules/bolt.md",
                    "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                    "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                ],
            },
            {
                "number": 1446,
                "files": [
                    {"filename": "src/components/logs/LogsDrawer.tsx"},
                    {"filename": "/src/lib/utils.ts"},
                ],
            },
            {"number": 1, "error": "not found", "changed_files": ["ignored.ts"]},
        ]

        self.assertEqual(
            _pr_candidate_target_files(contexts),
            [
                "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                "src/components/logs/LogsDrawer.tsx",
                "src/lib/utils.ts",
            ],
        )

    def test_pr_candidate_target_files_skip_stale_current_layout_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            current = repo_path / "packages" / "tandem-control-panel" / "src" / "pages" / "DashboardPage.tsx"
            current.parent.mkdir(parents=True)
            current.write_text("export function DashboardPage() {}\n", encoding="utf-8")
            contexts = [
                {
                    "number": 1459,
                    "files": [
                        {
                            "filename": "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                            "status": "modified",
                        },
                        {"filename": "src/lib/utils.ts", "status": "modified"},
                    ],
                },
            ]

            annotated = _annotate_pr_candidate_current_layout(contexts, repo_path)

            self.assertEqual(
                _pr_candidate_target_files(annotated),
                ["packages/tandem-control-panel/src/pages/DashboardPage.tsx"],
            )
            self.assertEqual(annotated[0]["stale_files"], ["src/lib/utils.ts"])
            self.assertEqual(
                _pr_candidate_unexpected_changed_files(
                    [{"pr_candidate_context": annotated}],
                    [
                        "packages/tandem-control-panel/src/pages/DashboardPage.tsx",
                        "src/lib/utils.ts",
                    ],
                ),
                ["src/lib/utils.ts"],
            )

    def test_pr_candidate_edit_goal_replaces_matrix_only_goal(self) -> None:
        goal = _pr_candidate_edit_goal("Produce a concise applicability matrix for each PR.")

        self.assertIn("Apply the still-relevant code changes", goal)
        self.assertIn("An applicability matrix alone is not sufficient", goal)

    def test_worker_failure_blocker_preserves_engine_empty_response_details(self) -> None:
        blocker = _worker_failure_blocker(
            [
                {
                    "worker_id": "worker-1",
                    "status": "failed",
                    "returncode": 1,
                    "failure_reason": "ENGINE_EMPTY_RESPONSE",
                    "blocker_kind": "engine_empty_response",
                    "engine": {
                        "session_id": "session-1",
                        "run_id": "run-engine-1",
                        "retry_count": 1,
                        "fallback_mode": "prompt_sync",
                    },
                }
            ]
        )

        self.assertEqual(blocker["kind"], "engine_empty_response")
        self.assertIn("session_id=session-1", blocker["detail"])
        self.assertIn("fallback=prompt_sync", blocker["detail"])

    def test_github_project_contract_target_files_override_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "unrelated.rs").write_text("fn unrelated() {}\n", encoding="utf-8")
            (repo_path / "crates").mkdir()
            task = {
                "title": "Add tenant helpers",
                "description": "\n".join(
                    [
                        "Add reusable tenant denial helpers",
                        "",
                        "## Files Likely Touched",
                        "- `crates/tandem-server/src/http/tests/mod.rs`",
                        "- `crates/tandem-server/src/app/state/tests/mod.rs`",
                    ]
                ),
                "source": {"type": "github_project", "issue_number": 1},
            }
            manager_plan = {
                "subtasks": [
                    {
                        "title": "wrong slice",
                        "files": ["src/unrelated.rs"],
                    }
                ]
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, manager_plan, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/tests/mod.rs",
                    "crates/tandem-server/src/app/state/tests/mod.rs",
                ],
            )

    def test_github_project_fallback_subtask_keeps_contract_target_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "unrelated.rs").write_text("fn unrelated() {}\n", encoding="utf-8")
            task = {
                "title": "Filter sessions by tenant",
                "description": "\n".join(
                    [
                        "Filter session CRUD routes by tenant.",
                        "",
                        "## Files Likely Touched",
                        "- `crates/tandem-server/src/http/sessions.rs`",
                        "- `crates/tandem-core/src/storage_parts/`",
                    ]
                ),
                "source": {"type": "github_project", "issue_number": 1428},
            }

            _, subtasks = _prepare_subtasks_with_discovery(task, {"subtasks": []}, repo_path, 1)

            self.assertEqual(
                subtasks[0]["files"],
                [
                    "crates/tandem-server/src/http/sessions.rs",
                    "crates/tandem-core/src/storage_parts/",
                ],
            )
            self.assertEqual(subtasks[0]["target_files"], subtasks[0]["files"])

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

    def test_verified_existing_short_circuit_rejects_github_project_tasks(self) -> None:
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
        self.assertFalse(
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

    def test_verified_existing_short_circuit_rejects_linear_code_edit_tasks(self) -> None:
        subtasks = [{"id": "subtask-1", "files": ["index.html"]}]
        worker_results = [{"subtask_id": "subtask-1", "status": "skipped_existing"}]

        self.assertFalse(
            _all_subtasks_verified_existing(
                subtasks,
                worker_results,
                {"ok": True},
                {"execution_kind": "code_edit", "source": {"type": "linear", "issue_id": "TAN-68"}},
            )
        )

    def test_write_required_worker_failure_is_unresolved_without_existing_proof(self) -> None:
        self.assertTrue(
            _has_unresolved_write_required_worker_failure(
                [{"subtask_id": "subtask-1", "status": "failed", "write_required": True}]
            )
        )
        self.assertFalse(
            _has_unresolved_write_required_worker_failure(
                [{"subtask_id": "subtask-1", "status": "failed", "write_required": False}]
            )
        )
        self.assertFalse(
            _has_unresolved_write_required_worker_failure(
                [
                    {
                        "subtask_id": "subtask-1",
                        "status": "failed",
                        "write_required": True,
                        "verified_existing": True,
                    }
                ]
            )
        )

    def test_worker_pool_preserves_subtask_write_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def worker_runner(*_args):
                return {
                    "status": "failed",
                    "returncode": 1,
                    "write_required": False,
                    "output_excerpt": "Worker reported success but produced no filesystem changes.",
                }

            results = _execute_local_worker_pool(
                resolve_config(root),
                "run-test",
                root,
                root,
                {"title": "Needs edits"},
                [
                    {
                        "id": "subtask-1",
                        "title": "Write the change",
                        "write_required": True,
                    }
                ],
                1,
                worker_runner=worker_runner,
            )

            self.assertEqual(results[0]["status"], "failed")
            self.assertTrue(results[0]["write_required"])
            self.assertTrue(_has_unresolved_write_required_worker_failure(results))

    def test_linear_comment_summary_records_pr_decisions(self) -> None:
        task = {
            "title": "Inventory Bolt/Jules PRs and record close/merge decision per PR",
            "raw_issue_body": "\n".join(
                [
                    "## Initial Inventory",
                    "",
                    "Green-ish / potentially cherry-pickable:",
                    "",
                    "* #1459 - 3+/3-, 3 files, green",
                    "* #1357 - 40+/34-, 2 files, green",
                    "",
                    "Large or suspicious despite green:",
                    "",
                    "* #1454 - 1507+/2857-, 5 files, green but too large for casual merge",
                    "",
                    "Failing and likely close/supersede unless valuable:",
                    "",
                    "* #1457, #1456, #1455",
                    "",
                    "## Acceptance",
                    "",
                    "* Decision notes are posted in this Linear issue or linked follow-up comments.",
                ]
            ),
            "acceptance_criteria": [
                "Decision notes are posted in this Linear issue or linked follow-up comments.",
            ],
        }

        summary = _linear_comment_task_summary(task)

        self.assertIn("#1459: cherry-pick", summary)
        self.assertIn("#1357: cherry-pick", summary)
        self.assertIn("#1454: needs-manual-review", summary)
        self.assertIn("#1457: close", summary)
        self.assertIn("#1456: close", summary)
        self.assertIn("No repository changes, commit, push, or PR were expected", summary)

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

    def test_collect_worker_changed_files_dedupes_and_rejects_parent_paths(self) -> None:
        files = _collect_worker_changed_files(
            [
                {"changed_files": ["./src/app.ts", "src/app.ts", "../outside.txt"]},
                {"changed_files": ["packages/panel/src/App.tsx", "safe/../unsafe.ts"]},
            ]
        )

        self.assertEqual(files, ["src/app.ts", "packages/panel/src/App.tsx"])

    def test_integration_blocker_detects_semantic_failure_with_zero_exit(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Duplicate findLast import remains.",'
                '"blockers":["missing PR"]}'
            ),
        }

        message = _integration_blocker_message(result)

        self.assertIsNotNone(message)
        self.assertIn("Integration review did not approve", message or "")
        self.assertIn("Duplicate findLast import remains", message or "")
        self.assertIn("missing PR", message or "")

    def test_integration_engine_watchdog_failure_defers_to_review(self) -> None:
        result = {
            "returncode": 1,
            "stdout": "ENGINE_TOOL_LOOP_STALLED: engine stalled after tool activity.",
        }

        self.assertTrue(_integration_failure_can_defer_to_review(result))

    def test_integration_plain_failure_does_not_defer_to_review(self) -> None:
        result = {
            "returncode": 1,
            "stdout": "integration command failed",
        }

        self.assertFalse(_integration_failure_can_defer_to_review(result))

    def test_integration_sandbox_inspection_blocker_defers_to_review(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Could not inspect repository status/diff because sandbox blocked git.",'
                '"tests":["Not run: git/status commands were blocked by bubblewrap_not_available."]}'
            ),
        }
        blocker = _integration_blocker_message(result) or ""

        self.assertTrue(_integration_semantic_blocker_can_defer_to_review(result, blocker))

    def test_integration_concrete_code_finding_does_not_defer(self) -> None:
        result = {
            "returncode": 0,
            "stdout": (
                '{"status":"blocked","approved":false,'
                '"summary":"Duplicate findLast import remains.",'
                '"required_fixes":["Remove the duplicate import."]}'
            ),
        }
        blocker = _integration_blocker_message(result) or ""

        self.assertFalse(_integration_semantic_blocker_can_defer_to_review(result, blocker))

    def test_preserve_and_reset_blocked_worktree_cleans_synced_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            artifacts = root / "artifacts"
            artifacts.mkdir()
            repo.mkdir()
            run_command(["git", "init", "--initial-branch=main", str(repo)])
            (repo / "README.md").write_text("before\n", encoding="utf-8")
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "add", "README.md"])
            run_command(["git", "-C", str(repo), "-c", "user.name=ACA", "-c", "user.email=tandem.invalid", "commit", "-m", "init"])
            (repo / "README.md").write_text("after\n", encoding="utf-8")
            (repo / "scratch.txt").write_text("temp\n", encoding="utf-8")
            ctx = SimpleNamespace(
                repo_path=repo,
                cfg=SimpleNamespace(env={}),
                layout={"artifacts": artifacts},
                blackboard={},
            )

            _preserve_and_reset_blocked_worktree(ctx, reason="test")

            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "before\n")
            self.assertFalse((repo / "scratch.txt").exists())
            patch_text = (artifacts / "blocked-working-diff.patch").read_text(encoding="utf-8")
            self.assertIn("-before", patch_text)
            self.assertIn("+after", patch_text)
            self.assertIn("blocked_worktree_cleanup", ctx.blackboard)

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
