from __future__ import annotations

import unittest

from src.tandem_agents.core.phases.review_verify import _verification_commands_from_task_text


class ReviewVerifyTest(unittest.TestCase):
    def test_verification_commands_from_task_text_extracts_fenced_shell_commands(self) -> None:
        task = {
            "description": (
                "Verification:\n\n"
                "```bash\n"
                "# focused smoke check\n"
                "python3 -m unittest src.tandem_agents.aca_harness.calculator_test\n"
                "```\n"
            )
        }

        commands = _verification_commands_from_task_text(task)

        self.assertEqual(commands, ["python3 -m unittest src.tandem_agents.aca_harness.calculator_test"])


if __name__ == "__main__":
    unittest.main()
