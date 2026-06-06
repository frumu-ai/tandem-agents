from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from src.tandem_agents.config.config_loader import resolve_config, validate_config
from src.tandem_agents.core.verification.review_policy import evaluate_review_policy


class ReviewPolicyTest(unittest.TestCase):
    def _config(self, root: Path, policy: str = "human_review"):
        (root / "agent.yaml").write_text(
            dedent(
                f"""
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
                  policy: {policy}
                output:
                  root: runs
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return resolve_config(root)

    def test_human_review_policy_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, policy="human_review")
            decision = evaluate_review_policy(cfg)

            self.assertTrue(decision.supported)
            self.assertTrue(decision.human_review_required)
            self.assertFalse(decision.auto_merge_requested)
            self.assertTrue(decision.merge_requires_approval)
            self.assertTrue(decision.branch_delete_requires_approval)
            self.assertTrue(decision.delete_branch_after_merge)
            self.assertIsNone(decision.blocker)
            self.assertTrue(any("human" in rule.lower() for rule in decision.handoff_rules))
            self.assertEqual(validate_config(cfg), [])

    def test_auto_merge_policy_is_supported_and_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._config(root, policy="auto_merge")
            decision = evaluate_review_policy(cfg)

            self.assertTrue(decision.supported)
            self.assertFalse(decision.human_review_required)
            self.assertTrue(decision.auto_merge_requested)
            self.assertTrue(decision.merge_requires_approval)
            self.assertTrue(decision.branch_delete_requires_approval)
            self.assertIsNone(decision.blocker)
            self.assertTrue(any("guard" in rule.lower() or "clean" in rule.lower() for rule in decision.handoff_rules))
            self.assertEqual(validate_config(cfg), [])


if __name__ == "__main__":
    unittest.main()
