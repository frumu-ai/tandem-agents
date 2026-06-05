# Task Sources

This document describes the normalized task inputs a portable auto-coder should understand.

In ACA installs, the control panel now owns the durable non-secret source settings in
`tandem-data/control-panel-config.json`. Environment variables still work as bootstrap or
override inputs, but the control panel is the preferred place to edit repo binding, task source,
provider defaults, and swarm policy.

## GitHub Project Item

Use when the task comes from a GitHub Project board. Tandem should resolve this source through its built-in GitHub MCP path.

When ACA wants to preview or route the task before intake, it should call `sdk_task_intake_preview(...)` and preserve any project name or column hints that come back.

Required fields:

- owner
- project number
- item number or item URL

Example:

```json
{
  "type": "github_project",
  "owner": "acme",
  "project_number": 7,
  "item_number": 42
}
```

## Linear Issue

Use when the task comes from Linear. Tandem resolves this source through the Linear MCP server connected in the Tandem control panel. ACA does not store a Linear API token; it uses the engine MCP registry and the OAuth-backed `linear` server.

Required fields:

- team key or team name

Optional fields:

- project name or id
- statuses to treat as launchable, comma-separated
- labels to filter intake, comma-separated
- query text
- item identifier, issue id, or issue URL for a specific issue

Example:

```json
{
  "type": "linear",
  "team": "ENG",
  "project": "Runtime",
  "statuses": "Backlog,Todo,Triage,Ready",
  "labels": "bug",
  "item": "ENG-123"
}
```

## Kanban Board

Use when the task comes from a local or persistent board file.

Required fields:

- path to the board file

Optional fields:

- card id if you want a specific card

Example:

```json
{
  "type": "kanban_board",
  "path": "/home/user/tandem-agents/config/board.yaml",
  "card_id": "card-123"
}
```

## Local Backlog Item

Use when the task is defined in a local markdown or text backlog.

Required fields:

- path to the backlog file
- item key or line identifier if available

Example:

```json
{
  "type": "local_backlog",
  "path": "/home/user/projects/app/TODO.md",
  "item_key": "fix-login-flow"
}
```

## Manual Prompt

Use when the operator gives the agent a direct instruction.

Required fields:

- prompt text

Example:

```json
{
  "type": "manual",
  "prompt": "Fix the failing auth test and explain the root cause."
}
```

## Custom Adapter

Use this for future adapters that do not fit the built-in shapes.

Required fields:

- source name
- payload

Example:

```json
{
  "type": "custom",
  "source_name": "linear",
  "payload": {
    "ticket_id": "LIN-123"
  }
}
```

## Prerequisites

Each task source has different requirements. Make sure the required dependencies are satisfied before running ACA.

### GitHub Project

**Requirements:**

- Tandem engine running and reachable from ACA
- Tandem engine has a connected GitHub MCP server
- ACA supports both the legacy GitHub Project MCP tool names and the newer `projects_get` / `projects_list` GitHub MCP tool family
- When using the newer GitHub MCP server, ACA can read schema from `projects_list(method=list_project_fields)` and items from `projects_list(method=list_project_items)`
- GitHub PAT available through `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_TOKEN`, or Tandem's persisted auth store
- PAT includes GitHub Project and repository permissions required for board read/intake operations
- If inbox/item reads fail, check Tandem engine MCP connection and token permissions first
- If `task_source.item` is not set, ACA will auto-pick the first actionable project item by status, preferring `Todo` / `TODOS`
- If the board returns items without a populated status value, ACA falls back to the first returned project item
- If Tandem returns a task-intake preview, ACA should treat `preferred_route` as a hint and keep the project context on the normalized task record
- ACA can scope GitHub MCP to intake/finalize only via `ACA_GITHUB_MCP_SCOPE`, so normal coding phases can run without GitHub MCP tools in context
- Finalize-time project status/comment sync is controlled by `ACA_GITHUB_REMOTE_SYNC`

**How to check:**

```bash
# Inside the container:
python3 -m src.tandem_agents.cli check-engine
python3 -m src.tandem_agents.cli next-task
```

**Fallback:** Use `kanban_board` task source to test ACA without GitHub integration.

### Linear

**Requirements:**

- Tandem engine running and reachable from ACA
- Tandem control panel has a connected Linear MCP server, usually named `linear`
- Linear OAuth grants access to the selected team and project
- `task_source.team` is set; `task_source.project`, `statuses`, `labels`, and `query` can narrow intake
- If `task_source.item` is not set, ACA auto-picks one scheduler-approved issue from launchable statuses
- ACA can scope Linear MCP to intake/finalize only via `ACA_LINEAR_MCP_SCOPE`
- Finalize-time Linear status/label/comment sync is controlled by `ACA_LINEAR_REMOTE_SYNC`

**How to check:**

```bash
python3 -m src.tandem_agents.cli check-engine
python3 -m src.tandem_agents.cli next-task
```

**Fallback:** Use `kanban_board` task source to test ACA without Linear integration.

### Kanban Board

**Requirements:**

- None beyond the board file existing at the configured path
- No external API access needed
- Useful for local testing and autonomous operation

### Manual Prompt

**Requirements:**

- None
- No external dependencies

## Operational Notes

- Convert the source into one normalized task record before editing code.
- Keep source metadata separate from repo binding.
- Do not start implementation until the task record is unambiguous.
