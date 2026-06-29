from __future__ import annotations

import unittest

from src.tandem_agents.core.repository.repo_graph_eval import EVAL_CASES, run_repo_graph_eval


class RepoGraphEvalTest(unittest.TestCase):
    def test_repo_graph_eval_fixtures_pass(self) -> None:
        result = run_repo_graph_eval()

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["score"], result["total"])
        self.assertEqual(result["total"], len(EVAL_CASES))


if __name__ == "__main__":
    unittest.main()
