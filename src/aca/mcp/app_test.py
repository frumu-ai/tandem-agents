from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.aca.mcp.app import create_app


class AcaMcpAppTest(unittest.TestCase):
    def test_describe_aca_requires_token_and_reports_runtime_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path = root / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)

            run_dir = root / "runs" / "run-20260101T000000Z-demo"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "summary.md").write_text("# Summary\n", encoding="utf-8")
            (run_dir / "status.json").write_text(
                "{"
                '"run":{"run_id":"run-20260101T000000Z-demo","status":"running"},'
                '"task":{"title":"Implement ACA overview MCP","source":{"type":"github_project"}},'
                '"repo":{"slug":"acme/demo","branch":"feature/overview"},'
                '"phase":{"name":"coder_execution"}'
                "}",
                encoding="utf-8",
            )

            env = {
                "ACA_ROOT": str(root),
                "ACA_API_TOKEN": "secret-token",
                "ACA_TASK_SOURCE_TYPE": "github_project",
                "ACA_TASK_SOURCE_OWNER": "acme",
                "ACA_TASK_SOURCE_PROJECT": "7",
                "ACA_TASK_SOURCE_ITEM": "42",
                "ACA_REPO_PATH": str(repo_path),
                "ACA_REPO_SLUG": "acme/demo",
                "ACA_REPO_URL": "https://github.com/acme/demo.git",
                "ACA_PROVIDER": "openai",
                "ACA_MODEL": "gpt-4.1-mini",
                "ACA_GITHUB_MCP_ENABLED": "true",
                "ACA_GITHUB_MCP_SCOPE": "intake_finalize",
                "ACA_GITHUB_REMOTE_SYNC": "status_comment",
            }

            app = create_app()

            with patch.dict("os.environ", env, clear=False):
                with patch("src.aca.mcp.snapshot.engine_status_report") as engine_mock:
                    engine_mock.return_value = {
                        "base_url": "http://127.0.0.1:39733",
                        "healthy": True,
                        "running": True,
                        "status": "running",
                        "version": "0.4.38",
                        "update_available": False,
                        "update_policy": "notify",
                        "startup_mode": "reuse_only",
                        "detail": "running engine",
                        "api_token_required": True,
                    }
                    with patch("src.aca.mcp.snapshot.get_mcp_server") as server_mock:
                        server_mock.return_value = {
                            "enabled": True,
                            "connected": True,
                            "transport": "https://api.githubcopilot.com/mcp/",
                        }
                        with patch("src.aca.mcp.snapshot.latest_run_dir") as latest_run_mock:
                            latest_run_mock.return_value = run_dir

                            with TestClient(app) as client:
                                unauthorized = client.get("/server.json")
                                self.assertIn(unauthorized.status_code, {401, 403})

                                headers = {"Authorization": "Bearer secret-token"}

                                manifest = client.get("/server.json", headers=headers)
                                self.assertEqual(manifest.status_code, 200, manifest.text)
                                self.assertEqual(manifest.json()["name"], "ac.tandem/aca-mcp")
                                self.assertTrue(manifest.json()["homepage"].endswith("/mcp"))

                                tools = client.post(
                                    "/mcp",
                                    headers=headers,
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                                )
                                self.assertEqual(tools.status_code, 200, tools.text)
                                tool_specs = tools.json()["result"]["tools"]
                                self.assertEqual([tool["name"] for tool in tool_specs], ["describe_aca"])
                                self.assertTrue(tool_specs[0]["annotations"]["readOnlyHint"])

                                describe = client.post(
                                    "/mcp",
                                    headers=headers,
                                    json={
                                        "jsonrpc": "2.0",
                                        "id": 2,
                                        "method": "tools/call",
                                        "params": {"name": "describe_aca", "arguments": {}},
                                    },
                                )
                                self.assertEqual(describe.status_code, 200, describe.text)
                                overview = describe.json()["result"]["overview"]
                                self.assertEqual(overview["auth"]["mode"], "bearer_api_key")
                                self.assertTrue(overview["validation"]["ok"])
                                self.assertEqual(overview["github_mcp"]["connected"], True)
                                self.assertEqual(overview["latest_run"]["run_id"], "run-20260101T000000Z-demo")
                                self.assertIn("intake_next_github_project_task", overview["allowed_next_actions"])
                                self.assertTrue(any(item["title"] == "GitHub Projects guide" for item in overview["doc_refs"]))

                                unknown = client.post(
                                    "/mcp",
                                    headers=headers,
                                    json={
                                        "jsonrpc": "2.0",
                                        "id": 3,
                                        "method": "tools/call",
                                        "params": {"name": "nope", "arguments": {}},
                                    },
                                )
                                self.assertEqual(unknown.status_code, 404)


if __name__ == "__main__":
    unittest.main()
