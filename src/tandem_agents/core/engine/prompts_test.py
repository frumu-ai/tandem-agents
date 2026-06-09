from __future__ import annotations

import unittest

from src.tandem_agents.core.engine.prompts import (
    _compact_pr_context,
    build_review_prompt,
    build_test_prompt,
    build_worker_prompt,
)

_TASK = {"title": "Add feature", "task_contract": {}}
_NOTES = [{"worker_id": "w1", "status": "completed"}]


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


if __name__ == "__main__":
    unittest.main()
