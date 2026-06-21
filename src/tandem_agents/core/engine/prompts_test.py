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

    def test_review_prompt_limits_scope_to_explicit_acceptance_criteria(self) -> None:
        prompt = build_review_prompt("run1", _TASK, _NOTES, repo_diff="diff --git a/app.py b/app.py\n")

        self.assertIn("Review only against the explicit task contract", prompt)
        self.assertIn("Do not expand scope from the title", prompt)
        self.assertIn("numbered checklist as controlling", prompt)


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

    def test_worker_prompt_uses_compact_docs_only_mode(self) -> None:
        subtask = self._subtask(
            title="Document smoke harness",
            goal="Create and link the smoke harness docs.",
            files=["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            existing_files=["docs/README.md"],
            acceptance_criteria=[
                "docs/ACA_SMOKE_HARNESS.md describes the harness purpose.",
                "docs/README.md links to docs/ACA_SMOKE_HARNESS.md.",
                "Run or attempt python3 -m unittest src.tandem_agents.aca_harness.calculator_test.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertLess(len(prompt), 3000)
        self.assertIn("Target docs: [\"docs/ACA_SMOKE_HARNESS.md\", \"docs/README.md\"]", prompt)
        self.assertIn("Keep the diff limited to the target docs", prompt)
        self.assertIn("python3 -m unittest src.tandem_agents.aca_harness.calculator_test", prompt)
        self.assertNotIn("private helpers", prompt)
        self.assertNotIn("Verification/coverage guardrail", prompt)
        self.assertNotIn("paired source+test", prompt)

    def test_worker_prompt_uses_compact_docs_only_mode_for_carried_repair(self) -> None:
        subtask = self._subtask(
            title="Document smoke harness",
            goal="Finish the preserved partial worker diff.",
            files=["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            target_files=["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            carry_forward_patch="/runs/run-1/artifacts/worker-1.patch",
            repair_worker_output_excerpt=(
                "Remaining implementation blockers: docs/README.md should link to docs/ACA_SMOKE_HARNESS.md."
            ),
            existing_files=["docs/ACA_SMOKE_HARNESS.md", "docs/README.md"],
            acceptance_criteria=[
                "The preserved docs diff is already applied.",
                "Finish any remaining declared docs target before broadening scope.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertLess(len(prompt), 3200)
        self.assertIn("Preserved docs patch status", prompt)
        self.assertIn("Continue from the current worktree state", prompt)
        self.assertIn("docs/README.md should link to docs/ACA_SMOKE_HARNESS.md", prompt)
        self.assertIn("Keep the diff limited to the target docs", prompt)
        self.assertNotIn("production-backed assertion", prompt)
        self.assertNotIn("private helpers", prompt)
        self.assertNotIn("paired source+test", prompt)

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
        self.assertIn("Do not stop after imports, constants, or scaffolding", prompt)
        self.assertIn("update the read path or config construction", prompt)

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
        self.assertIn("Prefer one focused write step that edits both", prompt)
        self.assertIn("production edit and test edit back-to-back", prompt)
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

        self.assertIn("replace a rejected production-only repair with one paired source+test diff", prompt)
        self.assertIn("Prefer one focused write step that edits both", prompt)
        self.assertIn("test edit and production edit back-to-back", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository_test.py", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository.py", prompt)

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

    def test_complementary_rejected_repair_prompt_requires_one_paired_diff(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_mode="complementary_rejected_partial_diff",
            repair_requires_paired_source_test_diff=True,
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=["src/tandem_agents/core/repository/repository_test.py"],
            discarded_partial_diff_patches=[
                "runs/run-1/artifacts/source.patch",
                "runs/run-1/artifacts/test.patch",
            ],
            write_required=True,
            acceptance_criteria=["Rebuild one paired source+test repair from clean files."],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("paired source+test diff", prompt)
        self.assertIn("single attempt", prompt)
        self.assertIn("Prefer one focused write step that edits both", prompt)
        self.assertIn("back-to-back before running searches", prompt)
        self.assertIn("production-only diff fails and a test-only diff fails", prompt)
        self.assertIn("substantive diff exists only after both", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository_test.py", prompt)
        self.assertIn("src/tandem_agents/core/repository/repository.py", prompt)
        self.assertNotIn("preserved test-only partial diff", prompt)

    def test_weak_source_test_precision_repair_prompt_requires_bounded_pair(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            discarded_partial_diff_patch="runs/run-1/artifacts/weak.patch",
            repair_changed_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=["src/tandem_agents/core/repository/repository_test.py"],
            repair_requires_paired_source_test_diff=True,
            repair_precision_edit=True,
            repair_diff_line_budget=80,
            repair_focus_instructions=["Produce one small paired source+test diff in this attempt."],
            write_required=True,
            acceptance_criteria=["Rebuild source+test coverage without replaying the weak patch."],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("paired source+test diff", prompt)
        self.assertIn("Precision repair budget", prompt)
        self.assertIn("under about 80 changed lines", prompt)
        self.assertIn("under about 80 changed diff lines", prompt)
        self.assertIn("do not rewrite or duplicate whole files", prompt)
        self.assertIn("Do not use whole-file write/overwrite tools", prompt)
        self.assertIn("Produce one small paired source+test diff", prompt)

    def test_weak_source_test_carry_forward_prompt_requires_one_paired_diff(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_mode="weak_source_test_diff",
            repair_requires_paired_source_test_diff=True,
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=["src/tandem_agents/core/repository/repository_test.py"],
            carry_forward_patch="/tmp/worker-1.partial-worker-diff.patch",
            write_required=True,
            acceptance_criteria=["Add a real assertion for the carried source+test diff."],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("one paired source+test diff", prompt)
        self.assertIn("tests=", prompt)
        self.assertIn("production=", prompt)
        self.assertNotIn("preserved test-only partial diff", prompt)

    def test_weak_source_test_live_repair_prompt_infers_pair_mode_from_text(self) -> None:
        subtask = self._subtask(
            title="Repair weak source+test partial diff",
            goal=(
                "Finish the preserved weak-test partial worker diff with production-backed regression coverage for "
                "src/tandem_agents/core/repository/repository.py, "
                "src/tandem_agents/core/repository/repository_test.py."
            ),
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=["src/tandem_agents/core/repository/repository_test.py"],
            carry_forward_patch="/tmp/worker-1.partial-worker-diff.patch",
            scope_note=(
                "ACA generated this repair plan deterministically after rejecting a source+test partial "
                "diff whose test changes lacked a real test method or assertion. The preserved weak source+test "
                "patch is applied before this worker starts so the retry can add missing assertion coverage."
            ),
            write_required=True,
            acceptance_criteria=[
                "The preserved weak source+test patch is applied before this worker starts.",
                "Make the first new repair edit in the required test file(s): "
                "src/tandem_agents/core/repository/repository_test.py; add a real test method or assertion.",
                "Treat the preserved source patch as the paired production behavior.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("one paired source+test diff", prompt)
        self.assertIn("tests=", prompt)
        self.assertIn("production=", prompt)
        self.assertNotIn("preserved test-only partial diff", prompt)
        self.assertNotIn("first new semantic edit in the paired production target", prompt)

    def test_complementary_repair_prompt_infers_pair_mode_from_live_repair_text(self) -> None:
        subtask = self._subtask(
            title="Rebuild complementary source and test partial diffs",
            goal=(
                "Rebuild a single source+test repair for "
                "src/tandem_agents/core/repository/repository.py, "
                "src/tandem_agents/core/repository/repository_test.py without copying either rejected partial patch."
            ),
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_requires_production_followup=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=["src/tandem_agents/core/repository/repository_test.py"],
            repair_failure_summary="previous retries produced separate source-only and test-only partial diffs",
            scope_note=(
                "ACA detected complementary rejected partial diffs: one source-only attempt and one test-only attempt. "
                "Rebuild both sides from clean files."
            ),
            write_required=True,
            acceptance_criteria=[
                "Make the first new semantic edit in the required test file(s): src/tandem_agents/core/repository/repository_test.py.",
                "Immediately pair that test edit with the smallest production edit in: src/tandem_agents/core/repository/repository.py.",
                "A production-only diff repeats WORKER_OFF_TRACK_TESTLESS_DIFF and fails this repair; a test-only diff repeats WORKER_TEST_ONLY_DIFF and fails this repair.",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("paired source+test diff", prompt)
        self.assertIn("Prefer one focused write step that edits both", prompt)
        self.assertNotIn("preserved test-only partial diff", prompt)
        self.assertNotIn("first new semantic edit in the paired production target", prompt)

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

    def test_worker_prompt_warns_against_brace_globs_for_target_files(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
            ],
            target_files=[
                "src/tandem_agents/core/phases/task_intake.py",
                "src/tandem_agents/core/repository/repository.py",
            ],
        )

        prompt = build_worker_prompt("run1", "worker-2", subtask, self._TASK, "/wt")

        self.assertIn("Do not combine concrete target files into brace-glob patterns", prompt)
        self.assertIn("retry the exact target path", prompt)

    def test_worker_prompt_forces_write_required_for_linear_code_edit_targets(self) -> None:
        task = {
            "title": "Linear code edit",
            "execution_kind": "code_edit",
            "source": {"type": "linear"},
            "task_contract": {},
        }
        subtask = self._subtask(
            files=["src/tandem_agents/core/phases/task_intake.py"],
            target_files=["src/tandem_agents/core/phases/task_intake.py"],
            write_required=False,
            pre_satisfied=False,
        )

        prompt = build_worker_prompt("run1", "worker-2", subtask, task, "/wt")

        self.assertIn("Write required for this worker: true", prompt)
        self.assertIn("This worker is write-required", prompt)

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

    def test_worker_prompt_compacts_repair_context(self) -> None:
        task = {
            "title": "TAN-170 Add isolated ACA worktrees",
            "task_contract": {
                "local_goal": "Implement repository isolation.",
                "target_files": [
                    "src/tandem_agents/core/repository/repository.py",
                    "src/tandem_agents/core/repository/repository_test.py",
                ],
                "notes_for_agent": "N" * 20_000,
            },
        }
        subtask = self._subtask(
            title="Repair testless partial diff",
            goal="G" * 20_000,
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            scope_note="S" * 20_000,
            acceptance_criteria=["A" * 20_000],
            discarded_partial_diff_patch="/runs/run-1/artifacts/source-only.patch",
            deterministic_partial_diff_repair=True,
            repair_changed_files=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=[
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_failure_summary="Worker drifted off the required regression path.",
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, task, "/wt")

        self.assertIn("Repair directive:", prompt)
        self.assertIn("Coverage/verification rule", prompt)
        self.assertIn("First read at least one required test target", prompt)
        self.assertIn("[truncated for worker prompt budget]", prompt)
        self.assertNotIn("N" * 5000, prompt)
        self.assertNotIn("S" * 5000, prompt)
        self.assertLess(len(prompt), 14_000)

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

    def test_worker_prompt_counts_carried_source_patch_for_test_followup(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            carry_forward_patch="/runs/run-1/artifacts/source.patch",
            repair_changed_files=["src/tandem_agents/core/repository/repository.py"],
            repair_requires_test_followup=[
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("carried source diff counts as the paired production edit", prompt)
        self.assertIn("Read and edit at least one required test target first", prompt)
        self.assertNotIn("Prefer one focused write step that edits both", prompt)

    def test_worker_prompt_includes_carry_forward_directive_for_single_preserved_patch(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            carry_forward_patch="/runs/run-1/artifacts/worker.patch",
            repair_changed_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_focus_instruction=(
                "Focused TypeError repair: production function(s) `worker_worktree_name` "
                "are being called with 3 positional arguments."
            ),
            repair_verification_first=True,
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Carry-forward repair directive:", prompt)
        self.assertIn("already applied the preserved partial patch data", prompt)
        self.assertIn("carried diff counts as the required working-tree change", prompt)
        self.assertIn("Run the focused verification first", prompt)
        self.assertIn("Immediate repair focus: Focused TypeError repair", prompt)
        self.assertIn("worker_worktree_name", prompt)
        self.assertIn("Failed-test repair rule", prompt)
        self.assertIn("inspect the current production function definitions", prompt)
        self.assertIn("do not invent a new branch/worktree/name format", prompt)
        self.assertNotIn("/runs/run-1/artifacts/worker.patch", prompt)

    def test_worker_prompt_includes_multiple_repair_focuses(self) -> None:
        subtask = self._subtask(
            files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            target_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            carry_forward_patch="/runs/run-1/artifacts/worker.patch",
            repair_changed_files=[
                "src/tandem_agents/core/repository/repository.py",
                "src/tandem_agents/core/repository/repository_test.py",
            ],
            repair_focus_instructions=[
                "Focused NameError repair: resolve undefined symbol(s) `_slug`.",
                "Focused TypeError repair: update production function(s) `task_run_branch_name` to accept `issue_id`.",
            ],
            repair_verification_first=True,
            write_required=True,
        )

        prompt = build_worker_prompt("run1", "worker-1", subtask, self._TASK, "/wt")

        self.assertIn("Immediate repair focus: Focused NameError repair", prompt)
        self.assertIn("_slug", prompt)
        self.assertIn("Immediate repair focus: Focused TypeError repair", prompt)
        self.assertIn("issue_id", prompt)
        self.assertIn("Failed-test repair rule", prompt)


if __name__ == "__main__":
    unittest.main()
