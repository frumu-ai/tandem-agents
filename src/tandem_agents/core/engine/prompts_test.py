from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.tandem_agents.core.engine.prompts import (
    _compact_pr_context,
    build_integration_prompt,
    build_manager_prompt,
    build_review_prompt,
    build_test_prompt,
    build_worker_prompt,
)

_TASK = {"title": "Add feature", "task_contract": {}}
_NOTES = [{"worker_id": "w1", "status": "completed"}]


class _StubConfig:
    env = {"ACA_PROVIDER": "openai-codex", "ACA_MODEL": "gpt-5.5"}
    provider = SimpleNamespace(base_url="")

    def provider_for_role(self, role: str) -> tuple[str, str]:
        return "openai-codex", "gpt-5.5"

    def provider_for_role_with_source(self, role: str) -> dict[str, str]:
        return {
            "provider": "openai-codex",
            "model": "gpt-5.5",
            "provider_source": "provider",
            "model_source": "provider",
        }


def _stub_config() -> _StubConfig:
    return _StubConfig()


class ReviewTestPromptDiffTest(unittest.TestCase):
    def test_review_prompt_embeds_provided_diff(self) -> None:
        diff = "diff --git a/app.py b/app.py\n+print('hi')\n"
        prompt = build_review_prompt("run1", _TASK, _NOTES, repo_diff=diff)
        self.assertIn("Uncommitted changes", prompt)
        self.assertIn("```diff", prompt)
        self.assertIn("print('hi')", prompt)

    def test_test_prompt_embeds_provided_diff(self) -> None:
        diff = "new file: app.py\nprint('hi')\n"
        prompt = build_test_prompt("run1", _TASK, {}, _NOTES, repo_diff=diff)
        self.assertIn("Uncommitted changes", prompt)
        self.assertIn("app.py", prompt)

    def test_test_prompt_embeds_inferred_verification_commands(self) -> None:
        prompt = build_test_prompt(
            "run1",
            _TASK,
            {},
            _NOTES,
            verification_commands=["pnpm -C packages/tandem-control-panel run build"],
        )

        self.assertIn("ACA inferred these verification commands", prompt)
        self.assertIn("pnpm -C packages/tandem-control-panel run build", prompt)
        self.assertIn("python3 -m unittest", prompt)
        self.assertIn("do not use bare `python`", prompt)

    def test_integration_prompt_prefers_python3_unittest_for_python_tests(self) -> None:
        prompt = build_integration_prompt("run1", _TASK, _NOTES)

        self.assertIn("python3 -m unittest", prompt)
        self.assertIn("do not use bare `python`", prompt)

    def test_review_prompt_notes_empty_diff(self) -> None:
        prompt = build_review_prompt("run1", _TASK, _NOTES, repo_diff="")
        self.assertIn("none detected", prompt)
        self.assertNotIn("```diff", prompt)

    def test_review_prompt_defaults_to_empty_diff(self) -> None:
        # Backwards-compatible call without the new argument must still work.
        prompt = build_review_prompt("run1", _TASK, _NOTES)
        self.assertIn("none detected", prompt)

    def test_review_prompt_does_not_require_finalization_artifacts(self) -> None:
        prompt = build_review_prompt("run1", _TASK, _NOTES, repo_diff="diff --git a/app.py b/app.py\n")

        self.assertIn("before final handoff/publish", prompt)
        self.assertIn("Do not require a PR branch", prompt)
        self.assertIn("worker applicability notes", prompt)


class CompactPrContextTest(unittest.TestCase):
    def test_drops_full_patch_keeps_excerpt_and_metadata(self) -> None:
        context = [
            {
                "number": 5,
                "title": "Optimize",
                "files": [
                    {"filename": "a.py", "patch": "X" * 9000, "patch_excerpt": "X" * 100},
                ],
            }
        ]
        compact = _compact_pr_context(context)
        file_entry = compact[0]["files"][0]
        self.assertNotIn("patch", file_entry)
        self.assertIn("patch_excerpt", file_entry)
        self.assertEqual(compact[0]["number"], 5)
        # Original input is not mutated.
        self.assertIn("patch", context[0]["files"][0])


class WorkerPromptPrRefsTest(unittest.TestCase):
    _TASK = {"title": "Consolidate PRs", "task_contract": {}}

    def _subtask(self, **extra):
        base = {"title": "Subtask 1", "goal": "do it", "files": []}
        base.update(extra)
        return base

    def test_worker_prompt_lists_fetched_refs(self) -> None:
        subtask = self._subtask(
            pr_candidate_context=[{"number": 5, "files": [{"filename": "a.py", "patch": "X" * 9000}]}],
            pr_candidate_context_artifact="runs/x/artifacts/pr_candidate_context.json",
            pr_candidate_refs=[{"number": 5, "ref": "refs/aca/pr-5", "ok": True}],
        )
        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")
        self.assertIn("refs/aca/pr-5", prompt)
        self.assertIn("cherry-pick", prompt)
        self.assertIn("pr_candidate_context.json", prompt)
        # The huge full patch must not be inlined; the compact summary is used.
        self.assertNotIn("X" * 9000, prompt)

    def test_worker_prompt_without_refs_still_references_artifact(self) -> None:
        subtask = self._subtask(
            pr_candidate_context=[{"number": 5, "files": [{"filename": "a.py"}]}],
            pr_candidate_context_artifact="pr_candidate_context.json",
        )
        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")
        self.assertIn("pr_candidate_context.json", prompt)
        self.assertNotIn("refs/aca/pr-", prompt)

    def test_worker_prompt_allows_fallback_readback_when_verification_tool_is_skipped(self) -> None:
        prompt = build_worker_prompt("run1", "worker-1", self._subtask(files=["src/app.py"]), self._TASK, "/wt")

        self.assertIn("retry a narrower readback if a tool is skipped", prompt)
        self.assertIn("then return the final completion note", prompt)
        self.assertIn("python3 -m unittest", prompt)
        self.assertIn("Do not treat missing `pytest` as a blocker", prompt)

    def test_worker_prompt_bounds_verbose_task_payloads(self) -> None:
        task = {
            "title": "TAN-999 Reduce timeout risk",
            "description": "D" * 20_000,
            "raw_issue_body": "R" * 20_000,
            "task_contract": {
                "local_goal": "Implement the smallest useful timeout reduction.",
                "target_files": ["src/tandem_agents/core/engine/prompts.py"],
                "notes_for_agent": "N" * 20_000,
            },
        }
        subtask = self._subtask(
            goal="G" * 20_000,
            acceptance_criteria=["A" * 20_000],
            deliverables=["B" * 20_000],
            target_files=["src/tandem_agents/core/engine/prompts.py"],
            pr_candidate_context=[
                {
                    "number": 42,
                    "files": [
                        {
                            "filename": "src/tandem_agents/core/engine/prompts.py",
                            "patch_excerpt": "P" * 20_000,
                        }
                    ],
                }
            ],
            pr_candidate_context_artifact="pr_candidate_context.json",
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, task, "/wt")

        self.assertIn("TAN-999 Reduce timeout risk", prompt)
        self.assertIn("Implement the smallest useful timeout reduction", prompt)
        self.assertIn("src/tandem_agents/core/engine/prompts.py", prompt)
        self.assertIn("[truncated for worker prompt budget]", prompt)
        self.assertLess(len(prompt), 30_000)
        self.assertNotIn("D" * 5000, prompt)
        self.assertNotIn("R" * 5000, prompt)
        self.assertNotIn("N" * 5000, prompt)
        self.assertNotIn("P" * 5000, prompt)

    def test_worker_prompt_subtask_contract_uses_active_subtask_files(self) -> None:
        task = {
            "title": "TAN-57 Add regression coverage",
            "task_contract": {
                "target_files": [
                    "crates/tandem-server/src/http/coder_parts/part05.rs",
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ]
            },
        }
        subtask = self._subtask(
            title="Schema drift regression",
            files=[
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
            task_contract={
                "target_files": [
                    "crates/tandem-server/src/http/coder_parts/part05.rs",
                    "crates/tandem-server/src/http/coder_parts/part09.rs",
                    "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                ]
            },
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, task, "/wt")
        subtask_contract = prompt.split("Subtask contract:\n", 1)[1].split("\n\nAcceptance criteria:", 1)[0]

        self.assertIn("crates/tandem-server/src/http/coder_parts/part09.rs", subtask_contract)
        self.assertIn("crates/tandem-server/src/http/tests/coder_parts/part09.rs", subtask_contract)
        self.assertNotIn("crates/tandem-server/src/http/coder_parts/part05.rs", subtask_contract)

    def test_worker_prompt_warns_about_git_ignored_targets(self) -> None:
        subtask = self._subtask(
            files=[],
            target_files=[],
            ignored_target_files=["docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md"],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Git-ignored target files", prompt)
        self.assertIn("docs/internal/SIGNAL_TRIAGE_PIPELINE_KANBAN.md", prompt)
        self.assertIn("cannot create a reviewable diff", prompt)

    def test_worker_prompt_requires_real_verification_path_without_import_side_effects(self) -> None:
        subtask = self._subtask(
            title="Verify quality gates",
            goal="Exercise end-to-end signal quality gates",
            files=["scripts/bug-monitor-external-log-intake-smoke.mjs"],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("exercise the existing production path", prompt)
        self.assertIn("standalone simulation", prompt)
        self.assertIn("importing it does not execute its CLI main routine", prompt)
        self.assertIn("tracked fixtures should stay deterministic", prompt)
        self.assertIn("Do not define the quality-gate rules inside the test", prompt)
        self.assertIn("Preserve existing live smoke/API behavior", prompt)

    def test_worker_prompt_rejects_test_only_regression_oracles(self) -> None:
        subtask = self._subtask(
            title="Add GitHub Projects regression coverage",
            goal="Cover schema drift and readiness behavior",
            files=["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
            target_files=["crates/tandem-server/src/http/tests/coder_parts/part09.rs"],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("each new assertion must exercise existing production functions", prompt)
        self.assertIn("test-only enum, constant, local helper, or string table", prompt)
        self.assertIn("not valid coverage", prompt)

    def test_manager_prompt_warns_against_duplicate_quality_gate_implementation(self) -> None:
        prompt = build_manager_prompt(
            "run1",
            {
                "title": "Verify Bug Monitor quality gates",
                "description": "End-to-end smoke against quality gates",
                "task_contract": {},
            },
            {"path": "/repo"},
            _stub_config(),
            repo_context="crates/tandem-server/src/bug_monitor/service.rs",
        )

        self.assertIn("existing product implementation", prompt)
        self.assertIn("standalone duplicate implementation", prompt)
        self.assertIn("live smoke/API path", prompt)
        self.assertIn("prefer 1-3 high-signal files", prompt)
        self.assertIn("no more than three concrete acceptance criteria", prompt)
        self.assertIn("graph-derived required edit files", prompt)
        self.assertIn("plan worker deliverables around those paths first", prompt)
        self.assertIn("discovery/read-only context", prompt)
        self.assertIn("read concrete files before changing code", prompt)

    def test_manager_prompt_enters_partial_diff_repair_mode(self) -> None:
        prompt = build_manager_prompt(
            "run1",
            {
                "title": "Repair preserved diff",
                "description": "Finish the partial patch",
                "task_contract": {},
            },
            {"path": "/repo"},
            _stub_config(),
            previous_feedback=(
                "CRITICAL: Worker attempt 3 failed with retryable blocker `worker_incomplete_diff`.\n"
                "Changed files from the failed attempt:\n- crates/eval/src/scoring.rs\n"
                "Preserved partial patch: `runs/x/artifacts/worker.patch`\n"
                "Worker output excerpt:\nRemaining implementation blockers: missing passes() method."
            ),
        )

        self.assertIn("PARTIAL-DIFF REPAIR MODE", prompt)
        self.assertIn("Return exactly one subtask", prompt)
        self.assertIn("missing passes() method", prompt)
        self.assertIn("limited to changed files only when", prompt)
        self.assertIn("Put the recovered blocker fixes in canonical `acceptance_criteria`", prompt)

    def test_manager_prompt_treats_engine_timeout_partial_diff_as_reusable(self) -> None:
        prompt = build_manager_prompt(
            "run1",
            {
                "title": "Repair preserved timeout diff",
                "description": "Finish the partial patch",
                "task_contract": {},
            },
            {"path": "/repo"},
            _stub_config(),
            previous_feedback=(
                "CRITICAL: Worker attempt 2 failed with retryable blocker `engine_prompt_timeout`.\n"
                "Changed files from the failed attempt:\n- src/tandem_agents/api/worktree_isolation.py\n"
                "Preserved partial patch: `runs/x/artifacts/worker.patch`\n"
                "Worker output excerpt:\n"
                "ENGINE_PROMPT_TIMEOUT: Tandem engine prompt_sync worker prompt did not finish within 300s.\n"
                "The partial diff is not treated as a completed worker result; retry or block with this evidence."
            ),
        )

        self.assertIn("PARTIAL-DIFF REPAIR MODE", prompt)
        self.assertIn("Treat a preserved patch from `ENGINE_PROMPT_TIMEOUT`", prompt)
        self.assertIn("not treated as a completed worker result` as rejection by itself", prompt)
        self.assertIn("preserved patch changed only tests", prompt)
        self.assertIn("paired production behavior", prompt)
        self.assertNotIn("use it only as failure evidence", prompt)

    def test_manager_prompt_expands_rejected_partial_diff_to_parent_targets(self) -> None:
        prompt = build_manager_prompt(
            "run1",
            {
                "title": "Repair rejected partial diff",
                "description": "Finish real regression coverage",
                "task_contract": {
                    "target_files": [
                        "crates/tandem-server/src/http/coder_parts/part05.rs",
                        "crates/tandem-server/src/http/coder_parts/part09.rs",
                        "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
                    ]
                },
            },
            {"path": "/repo"},
            _stub_config(),
            previous_feedback=(
                "CRITICAL: Worker attempt 3 failed with retryable blocker `worker_incomplete_diff`.\n"
                "Preserved partial patch: `runs/x/artifacts/worker.patch`\n"
                "Worker output excerpt:\n"
                "verification not run\n"
                "The partial diff is not treated as a completed worker result; retry or block with this evidence."
            ),
        )

        self.assertIn("PARTIAL-DIFF REPAIR MODE", prompt)
        self.assertIn("parent task target files needed", prompt)
        self.assertIn("use it only as failure evidence", prompt)
        self.assertIn("crates/tandem-server/src/http/tests/coder_parts/part09.rs", prompt)
        self.assertNotIn("additional target files until the preserved patch is terminal", prompt)

    def test_write_required_prompt_rejects_marker_files(self) -> None:
        subtask = self._subtask(
            files=["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
            target_files=["scripts/bug-monitor-external-log-intake-fixture.test.mjs"],
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Inspect the smallest relevant slice of a declared target file first", prompt)
        self.assertIn("Briefly read one declared target before editing it", prompt)
        self.assertIn("Required substantive write targets", prompt)
        self.assertIn("scripts/bug-monitor-external-log-intake-fixture.test.mjs", prompt)
        self.assertIn("Do not create marker files", prompt)
        self.assertIn("Do not use no-op patches, comment-only changes", prompt)
        self.assertIn("temporary files", prompt)
        self.assertIn("will fail review", prompt)

    def test_worker_prompt_front_loads_mechanical_slice_edits(self) -> None:
        subtask = self._subtask(
            title="scheduler budget config loader",
            goal="Wire exact scheduler budget and backpressure config fields.",
            files=["src/tandem_agents/config/config_loader.py"],
            target_files=["src/tandem_agents/config/config_loader.py"],
            write_required=True,
            scope_note=(
                "Mechanical slice 2 of 3 for throughput config controls. "
                "Edit only config_loader.py. Assume SchedulerConfig already has the exact fields."
            ),
            acceptance_criteria=[
                "Load ACA_SCHEDULER_MAX_CONCURRENT_WORKER_RUNS env var.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Mechanical deterministic slice fast path", prompt)
        self.assertIn('First read only the smallest relevant part of ["src/tandem_agents/config/config_loader.py"]', prompt)
        self.assertIn("make the first semantic edit in that target before inspecting unrelated files", prompt)
        self.assertIn("do not explore the parent task surface until after a real diff exists", prompt)

    def test_implementation_subtask_with_test_acceptance_does_not_force_test_first(self) -> None:
        subtask = self._subtask(
            title="Add repository worktree and branch lifecycle primitives",
            goal="Implement repository-layer operations needed to create dedicated branch worktrees.",
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            write_required=True,
            acceptance_criteria=[
                "Repository code can create a deterministic branch and worktree for a run.",
                "Repository tests cover branch and worktree creation.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Required substantive write targets", prompt)
        self.assertNotIn("This is a test/regression coverage subtask", prompt)
        self.assertNotIn("Read and edit at least one required test target first", prompt)
        self.assertIn("must keep test coverage paired with production behavior", prompt)
        self.assertIn("first behavioral edit in a paired production target", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository.py", prompt)

    def test_testless_diff_repair_prompt_requires_test_first(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            write_required=True,
            repair_worker_output_excerpt=(
                "WORKER_OFF_TRACK_TESTLESS_DIFF\n"
                "Worker drifted off the required regression/test coverage path: after 224s it had "
                "changed only non-test files while required test files were "
                "src/tandem_agents/core/repository/repository_test.py."
            ),
            acceptance_criteria=["Finish repository isolation with tests."],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("must satisfy required test coverage", prompt)
        self.assertIn("Read and edit at least one required test target first", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository_test.py", prompt)
        self.assertIn("A production-only diff fails this worker", prompt)

    def test_test_only_partial_repair_prompt_requires_production_first(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            write_required=True,
            acceptance_criteria=["Finish the preserved repository isolation tests."],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("preserved test-only partial diff", prompt)
        self.assertIn("first new semantic edit in the paired production target", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository.py", prompt)
        self.assertNotIn("Read and edit at least one required test target first", prompt)

    def test_write_required_prompt_treats_metadata_targets_as_support_only(self) -> None:
        subtask = self._subtask(
            files=["scripts/bug-monitor-external-log-intake-fixture.test.mjs", "package.json"],
            target_files=["scripts/bug-monitor-external-log-intake-fixture.test.mjs", "package.json"],
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Required substantive write targets", prompt)
        self.assertIn("Support targets", prompt)
        self.assertIn("A package.json-only or lockfile-only diff fails this worker", prompt)

    def test_write_required_prompt_treats_docs_as_support_when_code_targets_exist(self) -> None:
        subtask = self._subtask(
            files=[
                "crates/tandem-tools/src/lib_parts/part01.rs",
                "TESTING_UPDATES.md",
                "SECURITY.md",
                ".github/workflows/ci.yml",
            ],
            target_files=[
                "crates/tandem-tools/src/lib_parts/part01.rs",
                "TESTING_UPDATES.md",
                "SECURITY.md",
                ".github/workflows/ci.yml",
            ],
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn(
            'Required substantive write targets for this worker: ["crates/tandem-tools/src/lib_parts/part01.rs"]',
            prompt,
        )
        self.assertIn("TESTING_UPDATES.md", prompt)
        self.assertIn("Support targets", prompt)
        self.assertIn("may be updated only after a substantive target has a real diff", prompt)

    def test_worker_prompt_includes_scope_note(self) -> None:
        subtask = self._subtask(
            files=["crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs"],
            target_files=["crates/tandem-server/src/http/tests/bug_monitor_parts/part03.rs"],
            scope_note="ACA narrowed an overbroad one-worker target set.",
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("ACA scope note", prompt)
        self.assertIn("overbroad one-worker target set", prompt)

    def test_worker_prompt_includes_compact_rejected_partial_repair_directive(self) -> None:
        subtask = self._subtask(
            files=[
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
            target_files=[
                "crates/tandem-server/src/http/coder_parts/part09.rs",
                "crates/tandem-server/src/http/tests/coder_parts/part09.rs",
            ],
            discarded_partial_diff_patch="/runs/run-1/artifacts/worker-1.patch",
            repair_changed_files=["crates/tandem-server/src/http/coder_parts/part09.rs"],
            repair_failure_summary="verification did not run; the diff was not wired into the production path",
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Repair directive:", prompt)
        self.assertIn("previous partial diff was rejected", prompt)
        self.assertIn("do not apply or copy it as-is", prompt)
        self.assertIn("First actions: read the target files", prompt)
        self.assertIn("verification did not run", prompt)
        self.assertNotIn("/runs/run-1/artifacts/worker-1.patch", prompt)

    def test_worker_prompt_treats_applied_carry_forward_patch_as_current_diff(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/config/config_types.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            target_files=[
                "src/tandem_agents/config/config_types.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            discarded_partial_diff_patch="/runs/run-1/artifacts/rejected.patch",
            carry_forward_patches=[
                "/runs/run-1/artifacts/source.patch",
                "/runs/run-1/artifacts/test.patch",
            ],
            repair_changed_files=[
                "src/tandem_agents/config/config_types.py",
                "src/tandem_agents/config/config_loader_test.py",
            ],
            repair_failure_summary="source and test partials were verified separately",
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Carry-forward repair directive:", prompt)
        self.assertIn("already applied the preserved partial patch data", prompt)
        self.assertIn("Inspect the current target files and working diff only", prompt)
        self.assertNotIn("The previous partial diff was rejected; do not apply or copy it as-is", prompt)
        self.assertNotIn("/runs/run-1/artifacts/source.patch", prompt)
        self.assertNotIn("/runs/run-1/artifacts/test.patch", prompt)


if __name__ == "__main__":
    unittest.main()
