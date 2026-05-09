# ACA Kanban Board

This document defines the board contract ACA uses to organize work before and during a run.

## The Three Layers

- `config/board.yaml` is the durable task board.
- `runs/<run_id>/blackboard.yaml` is the run-scoped coordination space.
- `runs/<run_id>/status.json` is the machine-readable execution snapshot.

The board tells ACA what to do next. The blackboard tells the manager and workers how the current run is organized. The status file tells the monitor what phase the run is in.

## Board File

The board file is YAML and contains two top-level keys:

- `board`
- `cards`

Recommended columns:

- `backlog`
- `ready`
- `in_progress`
- `review`
- `test`
- `blocked`
- `done`

## Card Fields

Each card should include:

- `id`
- `title`
- `lane`
- `description`
- `acceptance_criteria`
- `labels`
- `priority`
- `source`
- `repo`
- `subtasks`
- `history`

The current runner may also add:

- `assigned_run_id`
- `claimed_by`
- `created_at_ms`
- `updated_at_ms`

## Claiming Rules

- ACA claims the first card in `ready` or `backlog` unless `task_source.card_id` is set.
- When a card is claimed, its lane becomes `in_progress`.
- The runner records the claim on the card with the current run id and claimant.
- A successful run moves the card to `done`.
- A failed worker, review, or test step moves the card to `blocked`.

## Multi-Task Swarms

ACA can work on multiple tasks at once, but the split of responsibilities stays strict:

- the board decides which tasks exist
- the blackboard decides how one run is coordinated
- isolated workspaces (managed by the execution backend) isolate worker edits

Recommended multi-task flow:

1. The orchestrator loads the active project or board snapshot.
2. It selects only `todo` items for intake.
3. It groups independent items into a bounded worker batch.
4. It asks the backend to create one isolated workspace per worker or task slice.
5. Each worker claims exactly one slice and writes only inside its own workspace.
6. The manager records progress on the run blackboard and board snapshot.
7. Review and test steps run after the worker batch finishes or when a slice is complete.

Important rules:

- `todo` is the only actionable lane.
- `in_progress`, `review`, `blocked`, and `done` remain visible for context.
- workers should not share a worktree unless the task is intentionally serialized.
- the blackboard should carry orchestration state, not source-code edits.

## How It Fits Task Sources

- `kanban_board` uses a persistent board file directly.
- `manual`, `local_backlog`, `custom`, and `github_project` normalize into a transient board for the run.
- The run snapshot still records the normalized board card so monitoring stays consistent.

## Monitoring

Use these commands to inspect the board-driven flow:

```bash
./scripts/monitor.sh
./scripts/monitor.sh --follow
./scripts/run.sh --validate
./scripts/run.sh --init-board
```

The monitor prints:

- current run status
- board snapshot
- blackboard snapshot
- recent events
- role-specific logs

## Practical Rule

Keep cards small enough that one manager can split them into a bounded set of worker subtasks.
If a card is too large to understand in one pass, split it before claiming it.
