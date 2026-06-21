from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.tandem_agents.core.phases.finalize import _enqueue_and_dispatch_pr
from src.tandem_agents.core.phases.pr_body import build_pull_request_body


class _Coordination:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue_outbox(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


class FinalizePhaseTest(unittest.TestCase):
    def _ctx(self, root: Path) -> SimpleNamespace:
        status_path = root / "status.json"
        return SimpleNamespace(
            run_id="run-1",
            task={
                "task_id": "TAN-1",
                "title": "Create PR",
                "description": "## Context\n\nUsers need enough PR context to review ACA work.",
                "acceptance_criteria": ["Describe why the change exists.", "List changed files."],
                "source": {"type": "linear", "identifier": "TAN-1", "url": "https://linear.app/frumu/issue/TAN-1"},
                "task_contract": {"verification_commands": ["cargo test -p tandem-agents"]},
            },
            branch_name="aca/test",
            coordination=_Coordination(),
            cfg=SimpleNamespace(repository=SimpleNamespace(default_branch="main", slug="acme/demo")),
            layout={"summary": root / "summary.md", "status": status_path, "events": root / "events.jsonl"},
            blackboard={},
            status={"task": {}},
            worker_results=[],
            expected_repo_files=[],
            repo_validation={},
            review_result={},
            test_result={},
            manager_plan={},
        )

    def test_enqueue_and_dispatch_pr_returns_false_without_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core._dispatch_outbox_now",
                return_value={"items": [{"kind": "github_pull_request.create", "status": "failed"}]},
            ):
                self.assertFalse(_enqueue_and_dispatch_pr(ctx, "diff"))

    def test_enqueue_and_dispatch_pr_returns_true_with_pr_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            with patch(
                "src.tandem_agents.core.execution.runner_core._dispatch_outbox_now",
                return_value={
                    "items": [
                        {
                            "kind": "github_pull_request.create",
                            "status": "dispatched",
                            "pr_url": "https://github.com/acme/demo/pull/1",
                            "payload": {"run_id": "run-1"},
                        }
                    ]
                },
            ):
                self.assertTrue(_enqueue_and_dispatch_pr(ctx, "diff"))
            self.assertEqual(ctx.blackboard["pull_request"], "https://github.com/acme/demo/pull/1")

    def test_build_pull_request_body_uses_run_context_not_skeletal_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.layout["summary"].write_text(
                "# Run completed\n\n- Worker completed\n- Review return code: `0`\n",
                encoding="utf-8",
            )
            ctx.manager_plan = {
                "summary": "Add deterministic PR body generation from ACA run artifacts.",
                "risks": [
                    {
                        "risk": "PR bodies can omit context when summary.md is skeletal.",
                        "mitigation": "Compose from task, workers, validation, and review notes.",
                    }
                ],
            }
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "Implement rich PR body",
                    "status": "completed",
                    "changed_files": ["src/tandem_agents/core/phases/finalize.py"],
                    "output_excerpt": "- Adds a structured PR body builder.\n- Includes validation details.",
                }
            ]
            ctx.repo_validation = {
                "command_checks": [
                    {
                        "command": "python3 -m unittest src.tandem_agents.core.phases.finalize_test",
                        "status": "pass",
                        "returncode": 0,
                    }
                ]
            }
            ctx.review_result = {
                "returncode": 0,
                "stdout": '{"notes":["Reviewed generated PR body content."]}',
            }
            ctx.test_result = {
                "returncode": 0,
                "stdout": '{"commands":[{"command":"python3 -m unittest","result":"pass"}],"results":{"unit":"pass"}}',
            }

            body = build_pull_request_body(ctx, " src/tandem_agents/core/phases/finalize.py | 120 +")

        self.assertIn("## Summary", body)
        self.assertIn("Add deterministic PR body generation", body)
        self.assertIn("## Why", body)
        self.assertIn("Users need enough PR context", body)
        self.assertIn("## What Changed", body)
        self.assertIn("`src/tandem_agents/core/phases/finalize.py`", body)
        self.assertIn("## Acceptance Coverage", body)
        self.assertIn("Describe why the change exists.", body)
        self.assertIn("## Verification", body)
        self.assertIn("python3 -m unittest src.tandem_agents.core.phases.finalize_test", body)
        self.assertIn("## Known Limitations", body)
        self.assertIn("summary.md is skeletal", body)
        self.assertIn("ACA run: `run-1`", body)

    def test_enqueue_and_dispatch_pr_queues_rich_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.manager_plan = {"summary": "Build a reviewer-facing PR description."}
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "Improve PR body",
                    "status": "completed",
                    "changed_files": ["src/tandem_agents/core/phases/finalize.py"],
                }
            ]
            with patch(
                "src.tandem_agents.core.execution.runner_core._dispatch_outbox_now",
                return_value={
                    "items": [
                        {
                            "kind": "github_pull_request.create",
                            "status": "failed",
                            "payload": {"run_id": "run-1"},
                        }
                    ]
                },
            ):
                self.assertFalse(_enqueue_and_dispatch_pr(ctx, "finalize.py | 1 +"))

            payload = ctx.coordination.enqueued[0]["payload"]
            body = payload["body"]
            self.assertIn("## Summary", body)
            self.assertIn("Build a reviewer-facing PR description.", body)
            self.assertIn("`src/tandem_agents/core/phases/finalize.py`", body)
            self.assertNotEqual(body.strip(), "ACA automated PR for task: Create PR")

    def test_build_pull_request_body_filters_repair_loop_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.task["title"] = "ACA smoke 02: add multiply support to calculator harness"
            ctx.manager_plan = {
                "kind": "complementary_guarded_partial_diff",
                "summary": (
                    "Deterministic repair for complementary one-sided guard partial diffs; "
                    "ACA will compose both saved patches before asking the worker to verify."
                ),
                "subtasks": [
                    {
                        "title": "Verify complementary source and test partial diffs",
                        "goal": (
                            "Verify and minimally fix the combined source+test repair for "
                            "src/tandem_agents/aca_harness/calculator.py."
                        ),
                    }
                ],
            }
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "Verify complementary source and test partial diffs",
                    "status": "completed",
                    "changed_files": [
                        "src/tandem_agents/aca_harness/calculator.py",
                        "src/tandem_agents/aca_harness/calculator_test.py",
                    ],
                }
            ]

            body = build_pull_request_body(ctx, "")

        self.assertIn("ACA completed task: ACA smoke 02: add multiply support to calculator harness.", body)
        self.assertIn("Updated the files listed below to satisfy the task contract.", body)
        self.assertIn("`src/tandem_agents/aca_harness/calculator.py`", body)
        self.assertNotIn("Deterministic repair", body)
        self.assertNotIn("Verify complementary source and test partial diffs", body)

    def test_build_pull_request_body_filters_stale_repair_risks_after_verified_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.task["title"] = "ACA smoke 03: add divide support with zero guard"
            ctx.manager_plan = {
                "summary": "ACA completed task: ACA smoke 03: add divide support with zero guard.",
                "risks": [
                    "The preserved source+test patch may still need a narrow fix before the focused verification passes.",
                ],
            }
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "Repair failed source+test partial diff",
                    "status": "completed",
                    "changed_files": [
                        "src/tandem_agents/aca_harness/calculator.py",
                        "src/tandem_agents/aca_harness/calculator_test.py",
                    ],
                    "verification_command": [
                        "python3",
                        "-m",
                        "unittest",
                        "src.tandem_agents.aca_harness.calculator_test",
                    ],
                    "verification_returncode": 0,
                }
            ]
            ctx.review_result = {"returncode": 0}
            ctx.test_result = {"returncode": 0}

            body = build_pull_request_body(ctx, "")

        self.assertNotIn("## Known Limitations", body)
        self.assertNotIn("may still need", body)
        self.assertIn("`src/tandem_agents/aca_harness/calculator.py`", body)

    def test_build_pull_request_body_filters_recovered_engine_fallback_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.task["title"] = "ACA smoke 04: add operation registry helper"
            ctx.task["acceptance_criteria"] = [
                "Add `available_operations()` returning a sorted tuple of supported operation names.",
                "Use a single internal registry for operation lookup where practical.",
            ]
            ctx.manager_plan = {
                "summary": (
                    "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync fallback timed out "
                    "after 90s following an empty async transcript."
                ),
                "risks": [
                    "Manager planning did not return a valid JSON object.",
                ],
            }
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "ACA smoke 04: add operation registry helper - remaining repo-context targets",
                    "status": "completed",
                    "changed_files": [
                        "src/tandem_agents/aca_harness/calculator.py",
                        "src/tandem_agents/aca_harness/calculator_test.py",
                    ],
                }
            ]
            ctx.review_result = {"returncode": 0}
            ctx.test_result = {"returncode": 0}

            body = build_pull_request_body(ctx, "")

        self.assertIn("ACA completed task: ACA smoke 04: add operation registry helper.", body)
        self.assertIn("Updated the files listed below to satisfy the task contract.", body)
        self.assertIn("Add 'available_operations()'", body)
        self.assertNotIn("ENGINE_PROMPT_TIMEOUT", body)
        self.assertNotIn("empty async transcript", body)
        self.assertNotIn("remaining repo-context", body)
        self.assertNotIn("Manager planning did not return", body)
        self.assertNotIn("## Known Limitations", body)

    def test_build_pull_request_body_lists_declared_fenced_verification_without_filler_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.task["title"] = "ACA smoke 05: document the smoke harness contract"
            ctx.task["description"] = (
                "Verification:\n\n"
                "```bash\n"
                "python3 -m unittest src.tandem_agents.aca_harness.calculator_test\n"
                "```\n"
            )
            ctx.task["acceptance_criteria"] = [
                "The PR body lists the changed files, the verification command, and why the smoke harness documentation exists."
            ]
            ctx.manager_plan = {
                "summary": "Document the deterministic ACA smoke harness.",
                "subtasks": [
                    {
                        "goal": "ACA smoke 05: document the smoke harness contract",
                    }
                ],
            }
            ctx.worker_results = [
                {
                    "worker_id": "worker-1",
                    "title": "ACA smoke 05: document the smoke harness contract",
                    "status": "completed",
                    "changed_files": ["docs/README.md", "docs/ACA_SMOKE_HARNESS.md"],
                    "output_excerpt": (
                        "- Adds a new `docs/ACA_SMOKE_HARNESS.md` contract.\n"
                        "- None visible from the provided diff excerpt.\n"
                    ),
                }
            ]
            ctx.review_result = {"returncode": 0}
            ctx.test_result = {"returncode": 0}

            body = build_pull_request_body(ctx, "")

        self.assertIn("Declared verification: `python3 -m unittest src.tandem_agents.aca_harness.calculator_test`", body)
        self.assertIn("Adds a new 'docs/ACA_SMOKE_HARNESS.md' contract.", body)
        self.assertNotIn("None visible from the provided diff excerpt", body)

    def test_build_pull_request_body_keeps_string_risks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp))
            ctx.manager_plan = {
                "summary": "Preserve manager risk notes.",
                "risks": [
                    "Real-engine regression baselines remain blocked by TAN-6.",
                    {
                        "risk": "Generated PR bodies can omit reviewer context.",
                        "mitigation": "Compose from structured run artifacts.",
                    },
                ],
            }

            body = build_pull_request_body(ctx, "")

        self.assertIn("## Known Limitations", body)
        self.assertIn("Real-engine regression baselines remain blocked by TAN-6.", body)
        self.assertIn("Generated PR bodies can omit reviewer context.", body)
        self.assertIn("Mitigation: Compose from structured run artifacts.", body)


if __name__ == "__main__":
    unittest.main()
