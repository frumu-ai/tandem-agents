from __future__ import annotations

import tempfile
import unittest
from textwrap import dedent
from pathlib import Path

from src.tandem_agents.config.config_loader import resolve_config, validate_config


class ConfigLoaderControlPanelOverlayTest(unittest.TestCase):
    def test_control_panel_config_overrides_agent_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "tandem-data").mkdir()
            (root / ".env").write_text(
                "TANDEM_CONTROL_PANEL_CONFIG_FILE=tandem-data/control-panel-config.json\n",
                encoding="utf-8",
            )
            (root / "config" / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: kanban_board
                      path: config/board.yaml
                    repository:
                      path: ""
                      slug: ""
                      clone_url: ""
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    swarm:
                      enabled: false
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "tandem-data" / "control-panel-config.json").write_text(
                """
                {
                  "control_panel": {
                    "mode": "aca",
                    "aca_compact_nav": false
                  },
                  "repository": {
                    "slug": "frumu-ai/hello-tandem"
                  },
                  "provider": {
                    "id": "openrouter",
                    "model": "minimax/minimax-m2.7"
                  },
                  "mcp_servers": {
                    "github": {
                      "enabled": true,
                      "transport": "https://api.githubcopilot.com/mcp/",
                      "headers": {
                        "X-MCP-Toolsets": "default,projects"
                      }
                    },
                    "kb": {
                      "enabled": true,
                      "transport": "http://127.0.0.1:39736/mcp"
                    }
                  }
                }
                """.strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(root)

            self.assertEqual(cfg.control_panel.mode, "aca")
            self.assertFalse(cfg.control_panel.aca_compact_nav)
            self.assertEqual(cfg.repository.slug, "frumu-ai/hello-tandem")
            self.assertEqual(cfg.provider.id, "openrouter")
            self.assertEqual(cfg.provider.model, "minimax/minimax-m2.7")
            self.assertIn("github", cfg.mcp_servers)
            self.assertIn("kb", cfg.mcp_servers)
            self.assertEqual(cfg.mcp_servers["github"]["transport"], "https://api.githubcopilot.com/mcp/")
            self.assertEqual(cfg.mcp_servers["kb"]["transport"], "http://127.0.0.1:39736/mcp")
            self.assertEqual(cfg.storage.profile, "local")
            self.assertEqual(cfg.review.policy, "human_review")

    def test_storage_profile_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: kanban_board
                      path: config/board.yaml
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(root, env={"ACA_STORAGE_PROFILE": "shared", "ACA_COORDINATION_POSTGRES_URL": "postgres://localhost/aca"})

            self.assertEqual(cfg.storage.profile, "shared")
            self.assertEqual(cfg.storage.postgres_url, "postgres://localhost/aca")
            self.assertEqual(cfg.coordination.backend, "postgres")
            self.assertEqual(validate_config(cfg), [])

    def test_artifact_store_root_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: artifact storage
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(root, env={"ACA_ARTIFACT_STORE_ROOT": "tandem-data/artifact-store"})

            self.assertEqual(cfg.artifact_store.root, "tandem-data/artifact-store")
            self.assertEqual(cfg.artifact_store_root(), (root / "tandem-data" / "artifact-store").resolve())

    def test_validate_rejects_inverted_heartbeat_lease_ratio(self) -> None:
        # heartbeat * 3 must be <= lease_ttl so that a single dropped heartbeat
        # doesn't cause spurious lease expiration.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: heartbeat ratio test
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(
                root,
                env={
                    "ACA_LEASE_TTL_SECONDS": "30",
                    "ACA_HEARTBEAT_INTERVAL_SECONDS": "20",
                },
            )
            errors = validate_config(cfg)
            self.assertTrue(
                any("heartbeat_interval_seconds * 3" in error for error in errors),
                f"Expected heartbeat-ratio error, got: {errors}",
            )

    def test_review_policy_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: manual
                      prompt: review policy
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(
                root,
                env={
                    "ACA_REVIEW_POLICY": "auto_merge",
                    "ACA_AUTO_MERGE_STRATEGY": "squash",
                    "ACA_AUTO_MERGE_ALLOWED_STRATEGIES": "squash,rebase",
                },
            )

            self.assertEqual(cfg.review.policy, "auto_merge")
            self.assertEqual(cfg.review.auto_merge_strategy, "squash")
            self.assertEqual(cfg.review.auto_merge_allowed_strategies, "squash,rebase")
            self.assertEqual(validate_config(cfg), [])

    def test_github_mcp_defaults_off_without_token_or_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: kanban_board
                      path: config/board.yaml
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (root / "tandem-data").mkdir()
            (root / "tandem-data" / "control-panel-config.json").write_text(
                """
                {
                  "mcp_servers": {
                    "github": {
                      "transport": "https://api.githubcopilot.com/mcp/",
                      "headers": {
                        "X-MCP-Toolsets": "default,projects"
                      }
                    }
                  }
                }
                """.strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(root)

            self.assertFalse(cfg.github_mcp.enabled)

    def test_coder_supervision_timing_can_come_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    tandem:
                      base_url: http://127.0.0.1:39733
                    task_source:
                      type: kanban_board
                      path: config/board.yaml
                    repository:
                      slug: frumu-ai/hello-tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(
                root,
                env={
                    "ACA_CODER_WAIT_TIMEOUT_SECONDS": "5400",
                    "ACA_CODER_POLL_INTERVAL_SECONDS": "20",
                    "ACA_CODER_SUPERVISOR_ENABLED": "false",
                    "ACA_CODER_SUPERVISOR_INTERVAL_SECONDS": "45",
                    "ACA_CODER_SUPERVISOR_BATCH_SIZE": "25",
                    "ACA_CODER_CANCEL_ON_SOURCE_TERMINAL": "false",
                },
            )

            self.assertEqual(cfg.execution.coder_wait_timeout_seconds, 5400)
            self.assertEqual(cfg.execution.coder_poll_interval_seconds, 20)
            self.assertFalse(cfg.execution.coder_supervisor_enabled)
            self.assertEqual(cfg.execution.coder_supervisor_interval_seconds, 45)
            self.assertEqual(cfg.execution.coder_supervisor_batch_size, 25)
            self.assertFalse(cfg.execution.coder_cancel_on_source_terminal)

    def test_github_mcp_token_file_does_not_become_repo_credential_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "github_token"
            token_file.write_text("secret\n", encoding="utf-8")
            (root / "agent.yaml").write_text(
                dedent(
                    """
                    agent:
                      name: ACA
                    task_source:
                      type: github_project
                      owner: frumu-ai
                      repo: tandem
                      project: 1
                    repository:
                      slug: frumu-ai/tandem
                    provider:
                      id: openai
                      model: gpt-4.1-mini
                    output:
                      root: runs
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = resolve_config(
                root,
                env={
                    "GITHUB_TOKEN_FILE": str(token_file),
                    "ACA_GITHUB_MCP_ENABLED": "true",
                },
            )

            self.assertTrue(cfg.github_mcp.enabled)
            self.assertEqual(cfg.repository.credential_file, "")


if __name__ == "__main__":
    unittest.main()
