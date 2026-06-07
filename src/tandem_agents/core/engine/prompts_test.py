from __future__ import annotations

import unittest

from src.tandem_agents.core.engine.prompts import build_review_prompt, build_test_prompt

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

    def test_review_prompt_notes_empty_diff(self) -> None:
        prompt = build_review_prompt("run1", _TASK, _NOTES, repo_diff="")
        self.assertIn("none detected", prompt)
        self.assertNotIn("```diff", prompt)

    def test_review_prompt_defaults_to_empty_diff(self) -> None:
        # Backwards-compatible call without the new argument must still work.
        prompt = build_review_prompt("run1", _TASK, _NOTES)
        self.assertIn("none detected", prompt)


if __name__ == "__main__":
    unittest.main()
