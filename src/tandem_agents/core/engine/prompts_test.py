from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.tandem_agents.core.engine.prompts import (
    _compact_pr_context,
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
        self.assertIn("graph-derived likely files", prompt)
        self.assertIn("discovery evidence, not final proof", prompt)
        self.assertIn("read concrete files before changing code", prompt)

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


if __name__ == "__main__":
    unittest.main()
