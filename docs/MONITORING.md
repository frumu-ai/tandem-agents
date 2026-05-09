# ACA Monitoring

This document describes how to observe what the portable auto-coder is doing while it runs.

## Goals

Monitoring should answer these questions quickly:

- What task is the agent working on?
- What repo is it editing?
- What provider and model did it choose?
- What role is currently active?
- Is the run blocked, failing, or making progress?
- What changed in the repo?

## Recommended Signals

The agent should continuously produce:

- human-readable stdout/stderr logs
- a structured `status.json` as defined in `RUN_STATE_SCHEMA.md`
- an append-only `events.jsonl` as defined in `RUN_STATE_SCHEMA.md`
- a `summary.md` handoff as defined in `RUN_STATE_SCHEMA.md`
- Tandem engine status in the run record
- Tandem health endpoint that was used
- role-specific log files when swarm mode is enabled
- board and blackboard snapshots
- git diffs at useful checkpoints

## Suggested Run Layout

```text
.tandem-agents/runs/<run_id>/
  status.json
  summary.md
  events.jsonl
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
  diffs/
    before.txt
    after.txt
```

## Event Types

The monitor should expect events like:

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
- `test.started`
- `test.completed`
- `test.failed`
- `run.completed`

## What To Watch First

For quick operator visibility, watch:

- current phase
- active role
- Tandem engine status and version
- Tandem health endpoint and readiness state
- selected provider/model
- repo path
- selected board card
- latest event
- last command
- last git diff
- current block reason

## Usage

Use `scripts/monitor.sh` to inspect run artifacts from the host:

```bash
./scripts/monitor.sh
./scripts/monitor.sh --run-dir .tandem-agents/runs/run_2026_03_21_001
./scripts/monitor.sh --follow
```

For live engine status and run monitoring, run the monitor inside the container:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli monitor --follow
docker compose logs -f aca
```

## Operational Note

The local run folder is the source of truth.
Stdout and `docker compose logs` are live views.
GitHub comments should be reserved for safe public handoffs.
See `COMMANDS.md` for concrete inspect and trigger entry points.
See `RUN_STATE_SCHEMA.md` for the exact file and event shapes.
See `ENGINE_MANAGEMENT.md` for the engine reuse and update policy.
