from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "tandem-mcp-bootstrap.js"


class TandemMcpBootstrapTest(unittest.TestCase):
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
            self.assertEqual(updated["github"]["name"], "github")
            self.assertEqual(updated["kb"]["name"], "kb")
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
            self.assertEqual(updated["github"]["name"], "github")
            self.assertEqual(updated["github"]["headers"]["Authorization"], "Bearer ghp_test_token")

    def test_bootstrap_preserves_auth_kind_and_oauth_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "control-panel-config.json"
            registry = root / "mcp_servers.json"
            source.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "linear": {
                                "enabled": True,
                                "transport": "https://mcp.linear.app/mcp",
                                "auth_kind": "oauth",
                                "oauth": {
                                    "provider_id": "linear",
                                    "token_endpoint": "https://api.linear.app/oauth/token",
                                    "client_id": "linear-client",
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
                }
            )

            subprocess.run(["node", str(SCRIPT)], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            updated = json.loads(registry.read_text(encoding="utf-8"))

            self.assertTrue(updated["linear"]["enabled"])
            self.assertEqual(updated["linear"]["auth_kind"], "oauth")
            self.assertEqual(updated["linear"]["oauth"]["provider_id"], "linear")


if __name__ == "__main__":
    unittest.main()
