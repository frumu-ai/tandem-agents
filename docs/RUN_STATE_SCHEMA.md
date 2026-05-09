# ACA Run State Schema

Last updated: 2026-03-21
Status: Draft

## Purpose

This document defines the concrete run-state files ACA writes during a run.

The local run directory is the source of truth for monitoring, debugging, and handoff.

## Run Directory Layout

The runner currently writes these files and folders:

```text
.tandem-agents/runs/<run_id>/
  status.json
  events.jsonl
  summary.md
  board.yaml
  board.md
  blackboard.yaml
  blackboard.md
  logs/
    manager.log
    worker-1.log
    reviewer.log
    tester.log
  isolated_workspaces/
    worker-1/
    worker-2/
  diffs/
    before.txt
    after.txt
```

Engine and backend-specific fields (engine connection, worktree paths, MCP binding details) are documented in `docs/internal/TANDEM_INTEGRATION_PLAN.md`, not here.

## `status.json`

`status.json` is the mutable snapshot for the run.
It is rewritten atomically as the run progresses.

### Top-Level Sections

The runner currently writes these top-level sections:

- `run`
- `task`
- `repo`
- `engine`
- `provider`
- `swarm`
- `phase`
- `blocker`
- `artifacts`
- `timestamps`
- `metrics`

### `run`

Required fields:

- `run_id`: string
- `status`: string
- `created_at_ms`: integer
- `updated_at_ms`: integer

Optional fields:

- `started_at_ms`: integer or null
- `completed_at_ms`: integer or null
- `owner`: string or null
- `source`: string or null

Current runner status values:

- `created`
- `running`
- `blocked`
- `completed`

Future values may appear as the workflow grows, but the monitor should treat unknown values as informational.

### `task`

Required fields:

- `title`: string
- `source`: object

Common optional fields:

- `task_id`: string or null
- `description`: string or null
- `acceptance_criteria`: array of strings
- `labels`: array of strings
- `priority`: string or null
- `subtasks`: array of objects
- `repo`: object

The runner normalizes all task sources into this shape before execution.

### `repo`

Required fields:

- `path`: string

Optional fields:

- `remote_name`: string or null
- `default_branch`: string or null
- `branch`: string or null
- `commit`: string or null
- `dirty`: boolean or null
- `worktree_root`: string or null
- `remote`: string or null

### `engine`

This section mirrors the execution backend status report returned during startup.

For Tandem-specific fields, see `docs/internal/TANDEM_INTEGRATION_PLAN.md`.

Common ACA-facing fields:

- `base_url`: string
- `status`: string
- `healthy`: boolean
- `version`: string or null
- `checked_at_ms`: integer

### `provider`

Required fields:

- `id`: string
- `model`: string

Optional fields:

- `fallback_provider`: string or null
- `fallback_model`: string or null

### `swarm`

Required fields:

- `enabled`: boolean
- `shared_model`: boolean
- `max_workers`: integer

Optional fields:

- `manager`: object or null
- `worker`: object or null
- `reviewer`: object or null
- `tester`: object or null
- `active_workers`: integer or null

### `phase`

Required fields:

- `name`: string
- `updated_at_ms`: integer

Optional fields:

- `detail`: string or null
- `role`: string or null

Current phase names:

- `bootstrap`
- `engine_check`
- `task_resolution`
- `worker_execution`
- `review`
- `test`
- `handoff`

### `blocker`

Required fields:

- `active`: boolean

Optional fields:

- `kind`: string or null
- `message`: string or null
- `owner_role`: string or null

### `artifacts`

`artifacts` is an object of path strings that point to the run outputs.

Current keys:

- `run_dir`
- `status_json`
- `events_jsonl`
- `summary_md`
- `board_yaml`
- `blackboard_yaml`
- `logs_dir`
- `worktrees_dir`
- `diffs_dir`

### `timestamps`

Required fields:

- `created_at_ms`: integer
- `updated_at_ms`: integer

Optional fields:

- `started_at_ms`: integer or null
- `completed_at_ms`: integer or null

### `metrics`

Current keys:

- `planned_workers`
- `completed_workers`
- `failed_workers`
- `tests_passed`

## `events.jsonl`

`events.jsonl` is the append-only event ledger for the run.
Each line is one JSON object.

### Required Fields

- `seq`: integer
- `type`: string
- `timestamp_ms`: integer
- `timestamp`: string
- `run_id`: string

### Optional Fields

- `task_id`: string or null
- `role`: string or null
- `repo`: object or null
- `payload`: object or null

### Current Event Types

The current runner emits these event types:

- `run.started`
- `run.blocked`
- `repo.resolved`
- `task.claimed`
- `manager.started`
- `manager.failed`
- `manager.completed`
- `swarm.spawned`
- `worker.started`
- `worker.completed`
- `worker.failed`
- `review.completed`
- `review.failed`
- `test.completed`
- `test.failed`
- `run.completed`

## `summary.md`

`summary.md` is the human handoff for the run.
It is written at the end of a successful run or a blocked run.

### Successful Run Shape

The current writer produces:

- `# Run completed`
- a short overview block with run id, task, repo, engine, and provider
- `## Workers`
- `## Validation`
- `## Diff Snapshot`
- `## Next Steps`

### Blocked Run Shape

The current writer produces:

- `# Run blocked`
- the task title
- a brief blocker explanation
- a short worker list when relevant

## Board And Blackboard Snapshots

These files help the monitor and the operator understand the run:

- `board.yaml` and `board.md` capture the board state for the current run
- `blackboard.yaml` and `blackboard.md` capture the run-scoped coordination state
- `logs/` captures per-role streamed output
- `isolated_workspaces/` isolates worker edits (backend-managed; see `docs/internal/TANDEM_INTEGRATION_PLAN.md`)
- `diffs/` captures the before and after repo status summary

For the canonical board and task objects used in the workspace schema, see `WORKSPACE_SCHEMA.md`.

## Write Rules

- write `status.json` atomically
- append to `events.jsonl` in order
- never rewrite events in place
- keep secrets out of run-state files
- keep human-readable text short and safe

## Example `status.json`

```json
{
  "run": {
    "run_id": "run-20260321T010101Z-abc123",
    "status": "running",
    "created_at_ms": 1742515200000,
    "updated_at_ms": 1742515380000,
    "started_at_ms": 1742515205000,
    "completed_at_ms": null,
    "owner": null,
    "source": null
  },
  "task": {
    "task_id": "card-123",
    "title": "Implement portable ACA runner",
    "description": "Make the local control plane runnable.",
    "source": {
      "type": "kanban_board",
      "board_path": "/home/user/tandem-agents/config/board.yaml",
      "card_id": "card-123"
    },
    "acceptance_criteria": []
  },
  "repo": {
    "path": "/home/user/tandem",
    "remote_name": "origin",
    "default_branch": "main",
    "branch": "main",
    "commit": "abc1234",
    "dirty": false,
    "worktree_root": "/home/user/tandem-agents/runs/repos",
    "remote": "origin\tgit@github.com:org/repo.git (fetch)"
  },
  "engine": {
    "base_url": "http://localhost:39733",
    "status": "running",
    "healthy": true,
    "running": true,
    "version": "0.4.8",
    "build_id": "0.4.8",
    "checked_at_ms": 1742515201000
  },
  "provider": {
    "id": "openai",
    "model": "gpt-4.1-mini"
  },
  "swarm": {
    "enabled": true,
    "shared_model": false,
    "max_workers": 3
  },
  "phase": {
    "name": "worker_execution",
    "updated_at_ms": 1742515380000,
    "detail": "workers are running",
    "role": "worker"
  },
  "blocker": {
    "active": false
  },
  "artifacts": {
    "run_dir": "/home/user/tandem-agents/runs/run-20260321T010101Z-abc123",
    "status_json": "/home/user/tandem-agents/runs/run-20260321T010101Z-abc123/status.json",
    "events_jsonl": "/home/user/tandem-agents/runs/run-20260321T010101Z-abc123/events.jsonl"
  },
  "timestamps": {
    "created_at_ms": 1742515200000,
    "updated_at_ms": 1742515380000,
    "started_at_ms": 1742515205000,
    "completed_at_ms": null
  },
  "metrics": {
    "planned_workers": 2,
    "completed_workers": 1,
    "failed_workers": 0,
    "tests_passed": null
  }
}
```

## Example Event

```json
{
  "seq": 7,
  "type": "worker.started",
  "timestamp_ms": 1742515340000,
  "timestamp": "2026-03-21T01:22:20Z",
  "run_id": "run-20260321T010101Z-abc123",
  "task_id": "card-123",
  "role": "worker",
  "repo": {
    "path": "/home/user/tandem"
  },
  "payload": {
    "worker_id": "worker-1",
    "subtask_id": "subtask-1"
  }
}
```

## Current Status

The runner and monitor both use this run-state shape today.
If a future release expands the schema, this document should be updated in lockstep with the code.
