from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tandem_agents.runtime.artifact_store import configure_artifact_store_root, mirror_run_file, mirror_run_tree, restore_run_tree


class ArtifactStoreTest(unittest.TestCase):
    def test_mirror_and_restore_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_root = root / "artifact-store"
            configure_artifact_store_root(store_root)
            run_dir = root / "runs" / "run-1"
            artifacts = run_dir / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "screenshot.png").write_bytes(b"png-bytes")
            (run_dir / "summary.md").write_text("# Summary\n", encoding="utf-8")

            self.assertIsNotNone(mirror_run_file("run-1", run_dir / "summary.md", "summary.md"))
            mirrored = mirror_run_tree("run-1", artifacts, logical_prefix="artifacts")
            self.assertEqual(len(mirrored), 1)

            restored_dir = root / "restore"
            restored = restore_run_tree("run-1", restored_dir)
            self.assertIn("summary.md", restored)
            self.assertIn("artifacts/screenshot.png", restored)
            self.assertEqual((restored_dir / "summary.md").read_text(encoding="utf-8"), "# Summary\n")
            self.assertEqual((restored_dir / "artifacts" / "screenshot.png").read_bytes(), b"png-bytes")


if __name__ == "__main__":
    unittest.main()
