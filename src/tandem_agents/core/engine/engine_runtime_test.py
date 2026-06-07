from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from src.tandem_agents.core.engine.engine_runtime import engine_visible_path


class EngineRuntimePathTest(unittest.TestCase):
    def test_engine_visible_path_maps_container_root_to_host_root(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "ACA_ROOT": "/workspace/tandem-agents",
                "ACA_ENGINE_HOST_ROOT": "/home/evan/tandem-agents",
            },
            clear=False,
        ):
            self.assertEqual(
                engine_visible_path(Path("/workspace/tandem-agents/runs/run-1")),
                Path("/home/evan/tandem-agents/runs/run-1"),
            )

    def test_engine_visible_path_is_noop_without_host_root(self) -> None:
        with mock.patch.dict("os.environ", {"ACA_ROOT": "/workspace/tandem-agents"}, clear=True):
            path = Path("/workspace/tandem-agents/runs/run-1")
            self.assertEqual(engine_visible_path(path), path)


if __name__ == "__main__":
    unittest.main()
