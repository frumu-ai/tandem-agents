from __future__ import annotations

import unittest

from src.tandem_agents.runtime.operator_dashboard import render_operator_dashboard


class OperatorDashboardTest(unittest.TestCase):
    def test_render_operator_dashboard_includes_tasks_runs_and_recovery(self) -> None:
        html = render_operator_dashboard(
            {
                "workspace": {"name": "Tandem Agents Workspace", "active_project_id": "alpha", "runs": [{"run_id": "run-1"}]},
                "coordination": {"backend": "sqlite", "summary": {"tasks": 1, "runs": 1, "workers": 1, "leases": 1, "outbox": 1}},
                "tasks": [
                    {
                        "title": "Implement thing",
                        "status": "running",
                        "ownership_state": "owned",
                        "worker": {"worker_id": "worker-1"},
                        "lease": {"lease_id": "lease-1"},
                        "execution_backend": "coder",
                        "execution_path": "tandem_coder",
                        "branch": "https://example.com/branch",
                        "pull_request": "https://example.com/pr/7",
                        "blocked_reason": "Need review",
                    }
                ],
                "runs": [
                    {
                        "run_id": "run-1",
                        "status": "running",
                        "execution_backend": "coder",
                        "admission_role": "aca_scheduler",
                        "execution_path": "tandem_coder",
                        "branch": "https://example.com/branch",
                        "pull_request": "https://example.com/pr/7",
                        "github_mcp": {"remote_sync": "status_comment"},
                    }
                ],
                "coder_runs": [
                    {
                        "run_id": "run-1",
                        "coder_run_id": "run-1",
                        "task_title": "Implement thing",
                        "repo_slug": "frumu-ai/example",
                        "status": "running",
                        "phase": "coder_execution",
                        "coder_supervision": {"tandem_status": "running", "tandem_phase": "coding"},
                    }
                ],
                "workers": [{"worker_id": "worker-1", "host_id": "host-a", "status": "idle", "last_seen_at_ms": 123, "current_lease_id": "lease-1", "capabilities": ["coder"]}],
                "leases": [{"lease_id": "lease-1", "task_key": "task-1", "worker_id": "worker-1", "host_id": "host-a", "status": "active", "expires_at_ms": 999}],
                "outbox": [],
                "scheduler_events": [{"event_type": "scheduler.dispatch", "created_at_ms": 1, "payload": {"policy": "fair_round_robin", "started": [1, 2]}}],
                "recovery": {"stale_workers": [{"worker_id": "worker-2", "host_id": "host-b", "last_seen_at_ms": 0}], "stale_leases": [], "failed_outbox": []},
                "generated_at_ms": 42,
            }
        )
        self.assertIn("ACA Operator Dashboard", html)
        self.assertIn("Implement thing", html)
        self.assertIn("worker-1", html)
        self.assertIn("lease-1", html)
        self.assertIn("tandem_coder", html)
        self.assertIn("Active Coder Runs", html)
        self.assertIn("coding", html)
        self.assertIn("Need review", html)
        self.assertIn("scheduler.dispatch", html)
        self.assertIn("Stale workers", html)


if __name__ == "__main__":
    unittest.main()
