from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "tandem-mcp-bootstrap.js"
HOSTED_RENDERER = ROOT / "scripts" / "hosted" / "render-control-panel-config.sh"


class TandemMcpBootstrapTest(unittest.TestCase):
    def test_renderer_emits_multiple_mcp_servers(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "HOSTED_DEPLOYMENT_NAME": "unit-test",
                "HOSTED_CONTROL_PANEL_PUBLIC_URL": "https://control.example",
                "HOSTED_ENABLE_GITHUB_MCP": "true",
                "HOSTED_GITHUB_MCP_URL": "https://api.githubcopilot.com/mcp/",
                "HOSTED_GITHUB_MCP_TOOLSETS": "default,projects",
                "HOSTED_GITHUB_MCP_SCOPE": "intake_finalize",
                "HOSTED_GITHUB_REMOTE_SYNC": "status_comment",
                "HOSTED_KB_ADMIN_URL": "http://tandem-kb-mcp:39736",
                "HOSTED_KB_MCP_URL": "http://tandem-kb-mcp:39736/mcp",
            }
        )

        rendered = subprocess.run(
            ["bash", str(HOSTED_RENDERER), "--deployment-name", "unit-test", "--public-url", "https://control.example"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        config = json.loads(rendered.stdout)

        self.assertIn("mcp_servers", config)
        self.assertIn("github", config["mcp_servers"])
        self.assertIn("kb", config["mcp_servers"])
        self.assertTrue(config["mcp_servers"]["github"]["enabled"])
        self.assertTrue(config["mcp_servers"]["kb"]["enabled"])
        self.assertEqual(config["mcp_servers"]["kb"]["transport"], "http://tandem-kb-mcp:39736/mcp")
        self.assertEqual(config["github_mcp"]["url"], "https://api.githubcopilot.com/mcp/")
        self.assertIn("hosted", config)
        self.assertTrue(config["hosted"]["managed"])
        self.assertEqual(config["hosted"]["public_url"], "https://control.example")
        self.assertEqual(config["hosted"]["control_plane_url"], "https://control.example")

    def test_bootstrap_preserves_explicit_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "control-panel-config.json"
            registry = root / "mcp_servers.json"
            token_file = root / "github_token"
            token_file.write_text("ghp_test_token\n", encoding="utf-8")
            source.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "github": {
                                "enabled": False,
                                "transport": "https://api.githubcopilot.com/mcp/",
                                "headers": {"X-MCP-Toolsets": "default,projects"},
                                "auth": {
                                    "token_file_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE"],
                                },
                            },
                            "kb": {
                                "enabled": True,
                                "transport": "http://127.0.0.1:39736/mcp",
                                "auto_connect": True,
                            },
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            registry.write_text(
                json.dumps(
                    {
                        "github": {
                            "enabled": False,
                            "connected": True,
                            "mcp_session_id": "stale-session",
                            "tool_cache": {"old": True},
                            "tools_fetched_at_ms": 1,
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "MCP_SOURCE_FILE": str(source),
                    "MCP_REGISTRY_FILE": str(registry),
                    "GITHUB_PERSONAL_ACCESS_TOKEN_FILE": str(token_file),
                }
            )

            subprocess.run(["node", str(SCRIPT)], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            updated = json.loads(registry.read_text(encoding="utf-8"))

            self.assertIn("github", updated)
            self.assertIn("kb", updated)
            self.assertFalse(updated["github"]["enabled"])
            self.assertFalse(updated["github"]["connected"])
            self.assertNotIn("mcp_session_id", updated["github"])
            self.assertNotIn("tool_cache", updated["github"])
            self.assertNotIn("tools_fetched_at_ms", updated["github"])
            self.assertEqual(updated["kb"]["transport"], "http://127.0.0.1:39736/mcp")
            self.assertTrue(updated["kb"]["enabled"])
            self.assertTrue(updated["kb"]["auto_connect"])

    def test_bootstrap_injects_token_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "control-panel-config.json"
            registry = root / "mcp_servers.json"
            token_file = root / "github_token"
            token_file.write_text("ghp_test_token\n", encoding="utf-8")
            source.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "github": {
                                "enabled": True,
                                "transport": "https://api.githubcopilot.com/mcp/",
                                "headers": {"X-MCP-Toolsets": "default,projects"},
                                "auth": {
                                    "token_file_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE"],
                                },
                            }
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "MCP_SOURCE_FILE": str(source),
                    "MCP_REGISTRY_FILE": str(registry),
                    "GITHUB_PERSONAL_ACCESS_TOKEN_FILE": str(token_file),
                }
            )

            subprocess.run(["node", str(SCRIPT)], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            updated = json.loads(registry.read_text(encoding="utf-8"))

            self.assertTrue(updated["github"]["enabled"])
            self.assertEqual(updated["github"]["headers"]["Authorization"], "Bearer ghp_test_token")


if __name__ == "__main__":
    unittest.main()
