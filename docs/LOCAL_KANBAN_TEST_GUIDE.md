# Local Kanban Test Guide

This guide walks through a simple end-to-end ACA test using:

- the ACA Docker stack
- a local Kanban board
- a small local git repo

Use this flow while GitHub Project intake or MCP integration is still being validated. It lets you prove:

- ACA can read a board
- ACA can bind to a repo
- ACA can execute a task with the configured engine/provider
- ACA can create run artifacts and a blackboard
- ACA can follow ordered work across multiple small cards

## Goal

Create a tiny test repo and a 4-step board for a web TODO app.

The suggested steps are:

1. create the app shell
2. add todo creation and rendering
3. add completion and deletion
4. persist todos and polish the UX

## Before You Start

Make sure the ACA Docker stack is running:

```bash
./scripts/build-containers.sh
```

For this guide, run ACA commands inside the container:

```bash
docker compose exec aca ...
```

## Step 1: Create A Small Test Repo

Create a local git repo on the host.

Example:

```bash
mkdir -p ~/aca-test/hello-tandem
cd ~/aca-test/hello-tandem
git init -b main
printf '# hello-tandem\n' > README.md
git add README.md
git commit -m "init"
```

ACA needs the repo mounted into the container at a known path.

ACA now includes a dedicated local repo mount:

- host path: `./test-repos`
- container path: `/workspace/test-repos`

So the easiest setup is:

```bash
mkdir -p ./test-repos
mv ~/aca-test/hello-tandem ./test-repos/hello-tandem
```

If you want a different host directory for local repos, set this in `.env`:

```env
ACA_LOCAL_REPOS_DIR=./test-repos
```

Then recreate the ACA container:

```bash
./scripts/build-containers.sh
```

Then point ACA at the mounted container path, not the host path.

Example `.env` values:

```env
ACA_TASK_SOURCE_TYPE=kanban_board
ACA_TASK_SOURCE_PATH=config/board.yaml
ACA_REPO_PATH=/workspace/test-repos/hello-tandem
ACA_DEFAULT_BRANCH=main
```

## Step 2: Initialize The Board

Create the board template inside ACA:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli init-board
```

This prepares `config/board.yaml` if it does not already exist.

## Step 3: Add The Test Cards

Edit `config/board.yaml` and add these cards.

Recommended board contents:

```yaml
board:
  columns:
    - backlog
    - ready
    - in_progress
    - review
    - test
    - blocked
    - done

cards:
  - id: todo-shell
    title: Create the app shell
    lane: ready
    description: >
      Create the initial single-page web app shell for hello-tandem with a title,
      input form area, todo list section, and empty state.
    acceptance_criteria:
      - App has a visible title and one-page layout
      - Page includes input area and todo list region
      - Empty state is visible when there are no todos
      - README includes local run instructions
    labels:
      - frontend
      - setup
    priority: high
    source:
      type: kanban_board
    repo:
      path: /workspace/test-repos/hello-tandem
    subtasks: []
    history: []

  - id: todo-create
    title: Add todo creation and rendering
    lane: backlog
    description: >
      Implement adding todos from the UI and rendering them in the list.
    acceptance_criteria:
      - User can type a todo and add it
      - Blank or whitespace-only todos are rejected
      - New todos render immediately in the list
    labels:
      - frontend
      - feature
    priority: high
    source:
      type: kanban_board
    repo:
      path: /workspace/test-repos/hello-tandem
    subtasks: []
    history: []

  - id: todo-state
    title: Add completion and deletion
    lane: backlog
    description: >
      Allow todos to be marked complete or incomplete and deleted from the list.
    acceptance_criteria:
      - Each todo can be marked complete
      - Completed todos are visually distinct
      - Each todo can be deleted
    labels:
      - frontend
      - state
    priority: medium
    source:
      type: kanban_board
    repo:
      path: /workspace/test-repos/hello-tandem
    subtasks: []
    history: []

  - id: todo-persistence
    title: Persist todos and polish UX
    lane: backlog
    description: >
      Persist todos in local storage and improve the basic UX.
    acceptance_criteria:
      - Todos persist across refresh
      - Saved todos are restored on load
      - Input clears after add
      - README describes persistence behavior
    labels:
      - frontend
      - polish
    priority: medium
    source:
      type: kanban_board
    repo:
      path: /workspace/test-repos/hello-tandem
    subtasks: []
    history: []
```

## Step 4: Validate The Setup

Check that ACA can see the repo binding and board:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli validate
docker compose exec aca python3 -m src.tandem_agents.cli next-task
```

Expected result:

- validation succeeds
- ACA selects `todo-shell` first

## Step 5: Run One Card

Run ACA once:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli run
```

Then inspect the result:

```bash
./scripts/monitor.sh
```

Or inside Docker:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli monitor
```

Expected result:

- a new run directory under `runs/`
- a `blackboard.yaml` created for that run
- a `status.json` snapshot
- the claimed card moved from `ready` to `done` or `blocked`

## Step 6: Advance The Next Card

This test uses simple ordered cards.

After the first card completes, move the next card from `backlog` to `ready` in `config/board.yaml`.

Then repeat:

```bash
docker compose exec aca python3 -m src.tandem_agents.cli next-task
docker compose exec aca python3 -m src.tandem_agents.cli run
```

Repeat until all 4 cards are complete.

## What This Test Proves

If this flow works, it proves ACA can already handle the core orchestration path:

- ordered task intake
- repo-scoped execution
- blackboard creation
- run-state recording
- sequential dependency handling through the board

It also gives you a clean local pattern for future multi-project testing, because you can place several repos under:

- `/workspace/test-repos/repo-a`
- `/workspace/test-repos/repo-b`
- `/workspace/test-repos/repo-c`

and point different boards or tasks at different repo paths.

It does not prove GitHub Project integration, but it does prove that the core ACA workflow works independently of GitHub transport details.

## Notes On Dependencies

For this first test, keep dependencies simple:

- only one card in `ready`
- all later dependent cards in `backlog`

That is enough to validate ordered work.

If you want to test richer dependency handling later, add explicit dependency fields in a future board format revision. For now, lane movement is the simplest and clearest way to enforce order.

## Recommended Operator Loop

1. Prepare the test repo.
2. Mount it into the ACA container.
3. Set `ACA_REPO_PATH` to the container path.
4. Set `ACA_TASK_SOURCE_TYPE=kanban_board`.
5. Initialize or edit `config/board.yaml`.
6. Run `next-task`.
7. Run ACA once.
8. Inspect the run output and blackboard.
9. Move the next dependent card into `ready`.
10. Repeat until the workflow is proven.
