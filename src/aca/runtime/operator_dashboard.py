from __future__ import annotations

from html import escape
from typing import Any, Mapping


def _text(value: Any) -> str:
    return escape(str(value or ""))


def _badge(label: Any, kind: str = "neutral") -> str:
    return f'<span class="badge badge-{_text(kind)}">{_text(label)}</span>'


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _summary_counts(summary: Mapping[str, Any]) -> dict[str, Any]:
    coordination = dict(summary.get("coordination") or {})
    coord_summary = dict(coordination.get("summary") or {})
    recovery = dict(summary.get("recovery") or {})
    tasks = list(summary.get("tasks") or [])
    runs = list(summary.get("runs") or [])
    coder_runs = list(summary.get("coder_runs") or [])
    workers = list(summary.get("workers") or [])
    leases = list(summary.get("leases") or [])
    outbox = list(summary.get("outbox") or [])
    return {
        "tasks": coord_summary.get("tasks", len(tasks)),
        "runs": coord_summary.get("runs", len(runs)),
        "coder_runs": len(coder_runs),
        "workers": coord_summary.get("workers", len(workers)),
        "leases": coord_summary.get("leases", len(leases)),
        "outbox": coord_summary.get("outbox", len(outbox)),
        "stale_workers": len(recovery.get("stale_workers") or []),
        "failed_outbox": len(recovery.get("failed_outbox") or []),
    }


def render_operator_dashboard(summary: Mapping[str, Any]) -> str:
    counts = _summary_counts(summary)
    workspace = dict(summary.get("workspace") or {})
    projects = list(summary.get("projects") or [])
    tasks = list(summary.get("tasks") or [])
    runs = list(summary.get("runs") or [])
    coder_runs = list(summary.get("coder_runs") or [])
    workers = list(summary.get("workers") or [])
    leases = list(summary.get("leases") or [])
    outbox = list(summary.get("outbox") or [])
    scheduler_events = list(summary.get("scheduler_events") or [])
    recovery = dict(summary.get("recovery") or {})
    generated_at_ms = summary.get("generated_at_ms")
    coordination = dict(summary.get("coordination") or {})

    def task_rows() -> str:
        rows: list[str] = []
        for task in tasks[:12]:
            status = str(task.get("status") or task.get("state") or "").strip().lower()
            dependency_status = dict(task.get("dependency_status") or {})
            contract_completeness = dict(task.get("contract_completeness") or {})
            badge_kind = "good" if status in {"done", "completed", "running", "active"} else "warn" if status in {"blocked", "stale"} or dependency_status.get("blocked") or not contract_completeness.get("ok", True) else "neutral"
            branch = task.get("branch") or "—"
            pull_request = task.get("pull_request") or "—"
            worker = task.get("worker") or {}
            lease = task.get("lease") or {}
            run = task.get("run_snapshot") or task.get("run") or {}
            title = _text(task.get("title") or task.get("task_key"))
            goal = str(task.get("local_goal") or task.get("program_goal") or "").strip()
            if goal:
                title += f'<div style="margin-top:4px;color:var(--muted);font-size:12px;">Goal: {_text(goal)}</div>'
            rows.append(
                _row(
                    [
                        title,
                        _badge(task.get("status") or task.get("state") or "unknown", badge_kind),
                        _text(task.get("ownership_state") or "unknown"),
                        _text(worker.get("worker_id") if isinstance(worker, dict) else "—"),
                        _text(lease.get("lease_id") if isinstance(lease, dict) else "—"),
                        _text(task.get("execution_backend") or "—"),
                        _text(task.get("execution_path") or "—"),
                        f"<code>{_text(branch)}</code>" if branch != "—" else "—",
                        f'<a href="{_text(pull_request)}">{_text(pull_request)}</a>' if pull_request != "—" else "—",
                        _text(
                            task.get("blocked_reason")
                            or dependency_status.get("blocked_reason")
                            or contract_completeness.get("blocker_message")
                            or run.get("error")
                            or "—"
                        ),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="10">No tasks found.</td></tr>'

    def run_rows() -> str:
        rows: list[str] = []
        for run in runs[:12]:
            branch = run.get("branch") or "—"
            pull_request = run.get("pull_request") or "—"
            github_sync = dict(run.get("github_mcp") or {})
            rows.append(
                _row(
                    [
                        _text(run.get("run_id")),
                        _badge(run.get("status") or "unknown", "good" if str(run.get("status") or "").strip().lower() in {"completed", "done"} else "warn" if str(run.get("has_error")) == "True" else "neutral"),
                        _text(run.get("execution_backend") or "—"),
                        _text(run.get("admission_role") or "—"),
                        _text(run.get("execution_path") or "—"),
                        f"<code>{_text(branch)}</code>" if branch != "—" else "—",
                        f'<a href="{_text(pull_request)}">{_text(pull_request)}</a>' if pull_request != "—" else "—",
                        _text(github_sync.get("remote_sync") or github_sync.get("scope") or "—"),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="8">No runs found.</td></tr>'

    def coder_run_rows() -> str:
        rows: list[str] = []
        for run in coder_runs[:12]:
            supervision = dict(run.get("coder_supervision") or {})
            rows.append(
                _row(
                    [
                        _text(run.get("run_id")),
                        _text(run.get("coder_run_id") or "—"),
                        _text(run.get("task_title") or "—"),
                        _badge(supervision.get("tandem_status") or run.get("status") or "running", "good" if str(supervision.get("tandem_status") or "").strip().lower() == "completed" else "neutral"),
                        _text(supervision.get("tandem_phase") or run.get("phase") or "—"),
                        _text(supervision.get("last_checked_at_ms") or "—"),
                        _text(run.get("repo_slug") or run.get("repo_path") or "—"),
                        _text(supervision.get("last_error") or "—"),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="8">No active coder runs.</td></tr>'

    def worker_rows() -> str:
        rows: list[str] = []
        for worker in workers[:12]:
            rows.append(
                _row(
                    [
                        _text(worker.get("worker_id")),
                        _text(worker.get("host_id") or "—"),
                        _badge(worker.get("status") or "unknown", "good" if str(worker.get("status") or "").strip().lower() == "idle" else "neutral"),
                        _text(worker.get("last_seen_at_ms") or "—"),
                        _text(worker.get("current_lease_id") or "—"),
                        _text(",".join(worker.get("capabilities") or []) if isinstance(worker.get("capabilities"), list) else worker.get("capabilities") or "—"),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="6">No workers found.</td></tr>'

    def lease_rows() -> str:
        rows: list[str] = []
        for lease in leases[:12]:
            rows.append(
                _row(
                    [
                        _text(lease.get("lease_id")),
                        _text(lease.get("task_key") or "—"),
                        _text(lease.get("worker_id") or "—"),
                        _text(lease.get("host_id") or "—"),
                        _badge(lease.get("status") or "unknown", "warn" if str(lease.get("status") or "").strip().lower() == "stale" else "neutral"),
                        _text(lease.get("expires_at_ms") or "—"),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="6">No leases found.</td></tr>'

    def recovery_rows() -> str:
        rows: list[str] = []
        for item in list(recovery.get("stale_leases") or [])[:8]:
            rows.append(_row([_text("stale lease"), _text(item.get("lease_id")), _text(item.get("task_key") or "—"), _text(item.get("worker_id") or "—")]))
        for item in list(recovery.get("stale_workers") or [])[:8]:
            rows.append(_row([_text("stale worker"), _text(item.get("worker_id")), _text(item.get("host_id") or "—"), _text(item.get("last_seen_at_ms") or "—")]))
        for item in list(recovery.get("failed_outbox") or [])[:8]:
            rows.append(_row([_text("failed outbox"), _text(item.get("outbox_id")), _text(item.get("kind") or "—"), _text(item.get("error") or "—")]))
        return "".join(rows) or '<tr><td colspan="4">No recovery items.</td></tr>'

    def event_rows() -> str:
        rows: list[str] = []
        for event in scheduler_events[:8]:
            payload = event.get("payload") if isinstance(event, dict) else {}
            rows.append(
                _row(
                    [
                        _text(event.get("event_type") or event.get("type") or "—"),
                        _text(event.get("created_at_ms") or "—"),
                        _text(payload.get("policy") if isinstance(payload, dict) else "—"),
                        _text(len(payload.get("started") or []) if isinstance(payload, dict) else "—"),
                    ]
                )
            )
        return "".join(rows) or '<tr><td colspan="4">No scheduler events.</td></tr>'

    html = f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>ACA Operator Dashboard</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #f6f7fb;
        --panel: #ffffff;
        --text: #16202a;
        --muted: #5c6570;
        --line: #d8dee8;
        --accent: #16324f;
        --good: #1b7f5a;
        --warn: #9a6700;
      }}
      body {{
        margin: 0;
        font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(180deg, #eef2f8 0%, var(--bg) 100%);
        color: var(--text);
      }}
      main {{
        max-width: 1380px;
        margin: 0 auto;
        padding: 24px;
      }}
      h1, h2, h3, p {{ margin: 0; }}
      .hero {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 20px 24px;
        box-shadow: 0 8px 30px rgba(20, 35, 58, 0.06);
      }}
      .meta {{
        margin-top: 12px;
        color: var(--muted);
        display: grid;
        gap: 6px;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      }}
      .stats {{
        margin: 18px 0 24px;
        display: grid;
        gap: 14px;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      }}
      .stat {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 16px;
      }}
      .stat .value {{
        font-size: 28px;
        font-weight: 700;
        line-height: 1.1;
      }}
      .stat .label {{
        color: var(--muted);
        font-size: 13px;
        margin-top: 6px;
      }}
      section {{
        margin: 18px 0;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 20px;
        padding: 18px;
        overflow-x: auto;
      }}
      section h2 {{
        margin-bottom: 10px;
        font-size: 18px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        min-width: 900px;
      }}
      th, td {{
        text-align: left;
        padding: 10px 12px;
        border-top: 1px solid var(--line);
        vertical-align: top;
        font-size: 14px;
      }}
      th {{
        border-top: none;
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
      }}
      .badge {{
        display: inline-block;
        padding: 4px 8px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
        white-space: nowrap;
      }}
      .badge-good {{ background: rgba(27, 127, 90, 0.12); color: var(--good); }}
      .badge-warn {{ background: rgba(154, 103, 0, 0.12); color: var(--warn); }}
      .badge-neutral {{ background: rgba(93, 101, 112, 0.10); color: var(--muted); }}
      a {{
        color: var(--accent);
        text-decoration: none;
        word-break: break-all;
      }}
      a:hover {{ text-decoration: underline; }}
      .footer {{
        margin: 16px 0 40px;
        color: var(--muted);
        font-size: 13px;
      }}
    </style>
  </head>
  <body>
    <main>
      <div class="hero">
        <h1>ACA Operator Dashboard</h1>
        <p style="margin-top:8px;color:var(--muted);">Compact live summary of coordination, runs, workers, leases, GitHub sync, and recovery state.</p>
        <div class="meta">
          <div><strong>Workspace:</strong> {_text(workspace.get("name") or workspace.get("id") or "unknown")}</div>
          <div><strong>Active project:</strong> {_text(workspace.get("active_project_id") or "none")}</div>
          <div><strong>Coordination backend:</strong> {_text(coordination.get("backend") or "unknown")}</div>
          <div><strong>Generated:</strong> {_text(generated_at_ms)}</div>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="value">{_text(counts["tasks"])}</div><div class="label">Tasks</div></div>
        <div class="stat"><div class="value">{_text(counts["runs"])}</div><div class="label">Runs</div></div>
        <div class="stat"><div class="value">{_text(counts["coder_runs"])}</div><div class="label">Active coder runs</div></div>
        <div class="stat"><div class="value">{_text(counts["workers"])}</div><div class="label">Workers</div></div>
        <div class="stat"><div class="value">{_text(counts["leases"])}</div><div class="label">Leases</div></div>
        <div class="stat"><div class="value">{_text(counts["outbox"])}</div><div class="label">Outbox rows</div></div>
        <div class="stat"><div class="value">{_text(counts["stale_workers"])}</div><div class="label">Stale workers</div></div>
        <div class="stat"><div class="value">{_text(counts["failed_outbox"])}</div><div class="label">Failed outbox rows</div></div>
      </div>

      <section>
        <h2>Tasks</h2>
        <table>
          <thead>
            <tr>
              <th>Task</th><th>Status</th><th>Ownership</th><th>Worker</th><th>Lease</th><th>Backend</th><th>Path</th><th>Branch</th><th>PR</th><th>Blocked</th>
            </tr>
          </thead>
          <tbody>{task_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Runs</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th><th>Status</th><th>Backend</th><th>Admission</th><th>Path</th><th>Branch</th><th>PR</th><th>GitHub sync</th>
            </tr>
          </thead>
          <tbody>{run_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Active Coder Runs</h2>
        <table>
          <thead>
            <tr>
              <th>ACA Run</th><th>Tandem Run</th><th>Task</th><th>Tandem status</th><th>Phase</th><th>Last check</th><th>Repo</th><th>Last error</th>
            </tr>
          </thead>
          <tbody>{coder_run_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Workers</h2>
        <table>
          <thead>
            <tr>
              <th>Worker</th><th>Host</th><th>Status</th><th>Last seen</th><th>Lease</th><th>Capabilities</th>
            </tr>
          </thead>
          <tbody>{worker_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Leases</h2>
        <table>
          <thead>
            <tr>
              <th>Lease</th><th>Task</th><th>Worker</th><th>Host</th><th>Status</th><th>Expires</th>
            </tr>
          </thead>
          <tbody>{lease_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Recovery</h2>
        <table>
          <thead>
            <tr>
              <th>Type</th><th>ID</th><th>Related</th><th>Detail</th>
            </tr>
          </thead>
          <tbody>{recovery_rows()}</tbody>
        </table>
      </section>

      <section>
        <h2>Scheduler Events</h2>
        <table>
          <thead>
            <tr>
              <th>Event</th><th>Created</th><th>Policy</th><th>Started</th>
            </tr>
          </thead>
          <tbody>{event_rows()}</tbody>
        </table>
      </section>

      <div class="footer">
        Projects: {_text(len(projects))} · Runs tracked in workspace: {_text(len((workspace.get("runs") or [])))} · Board files stay internal.
      </div>
    </main>
  </body>
</html>
"""
    return html
