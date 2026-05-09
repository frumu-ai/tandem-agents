# ACA Docs

This directory contains the planning, schema, and workflow docs for ACA.

Project files live one level up in the repository tree:

- [../README.md](../README.md) - root pointer to this docs index
- [../AGENTS.md](../AGENTS.md) - agent instructions
- [../config/agent.yaml](../config/agent.yaml) - machine-readable operator profile
- [../config/board.yaml](../config/board.yaml) - default Kanban board template
- [../src/tandem_agents/cli.py](../src/tandem_agents/cli.py) - CLI entry point
- [../src/tandem_agents/config.py](../src/tandem_agents/config.py) - config resolution and validation
- [../src/tandem_agents/config_loader.py](../src/tandem_agents/config_loader.py) - config loading and validation logic
- [../src/tandem_agents/config_types.py](../src/tandem_agents/config_types.py) - config dataclasses and helpers
- [../src/tandem_agents/utils.py](../src/tandem_agents/utils.py) - shared filesystem and timestamp helpers
- [../src/tandem_agents/board.py](../src/tandem_agents/board.py) - board file helpers
- [../src/tandem_agents/runstate.py](../src/tandem_agents/runstate.py) - run-state and blackboard helpers
- [../src/tandem_agents/task_sources.py](../src/tandem_agents/task_sources.py) - task-source normalization
- [../src/tandem_agents/prompts.py](../src/tandem_agents/prompts.py) - role prompt builders
- [../src/tandem_agents/worker.py](../src/tandem_agents/worker.py) - worker execution helpers
- [../src/tandem_agents/run_output.py](../src/tandem_agents/run_output.py) - status, snapshot, and summary writers
- [../src/tandem_agents/runner_core.py](../src/tandem_agents/runner_core.py) - orchestration logic
- [../src/tandem_agents/engine.py](../src/tandem_agents/engine.py) - Tandem engine and repo helpers
- [../src/tandem_agents/runner.py](../src/tandem_agents/runner.py) - orchestration and swarm runner
- [../src/tandem_agents/monitor.py](../src/tandem_agents/monitor.py) - run monitor and live follow mode
- [../src/tandem_agents/state.py](../src/tandem_agents/state.py) - board, run-state, and artifact helpers
- [../requirements.txt](../requirements.txt) - Python dependencies
- [../scripts/run.sh](../scripts/run.sh) - local launcher stub
- [../scripts/setup.sh](../scripts/setup.sh) - bootstrap and migration helper for `.env` and control-panel config
- [../scripts/build-containers.sh](../scripts/build-containers.sh) - Compose build helper that resolves the latest Tandem release by default
- [../scripts/monitor.sh](../scripts/monitor.sh) - local run inspector
- [../scripts/tandem-engine-serve.sh](../scripts/tandem-engine-serve.sh) - Tandem sidecar wrapper that reads the token file
- [../docker-compose.yml](../docker-compose.yml) - containerized runtime draft
- [../config/Dockerfile](../config/Dockerfile) - container image draft
- [../config/Dockerfile.engine](../config/Dockerfile.engine) - engine-only sidecar image
- [../.dockerignore](../.dockerignore) - Docker build context exclusions
- [../scripts/entrypoint.sh](../scripts/entrypoint.sh) - clone-and-prepare flow for containers
- [../.env.example](../.env.example) - local configuration template
- [../secrets/](../secrets/) - local mounted secret files, gitignored

## Files

- `PORTABLE_AUTOCODER_AGENT_PLAN.md` - design note for the portable auto-coder
- `TASK_SOURCES.md` - task source contract and examples
- `KANBAN_BOARD.md` - board, blackboard, and run coordination contract
- `REPO_BINDING.md` - repo selection and worktree guidance
- `CONFIG_SCHEMA.md` - canonical config contract for YAML and env
- `ENGINE_MANAGEMENT.md` - Tandem engine reuse, start, and update policy
- `COMMANDS.md` - operator commands for checking and triggering actions
- `RUN_STATE_SCHEMA.md` - exact run-state contract for status and events
- `CODING_TASKS_WITH_TANDEM.md` - coding-task execution loop for Tandem-backed repository edits
- `DOCKER_COMPOSE.md` - containerized startup notes
- `MONITORING.md` - how to watch runs and logs
- `TANDEM_CONTROL_PANEL_INTEGRATION.md` - how to connect external control panels
- `LOCAL_KANBAN_TEST_GUIDE.md` - practical Docker-based test flow using a local board and a small repo
- `kb_mcp.md` - KB MCP agent-guide upgrade plan and kanban board
- `TANDEM_KB_MCP_AND_ACA_OVERVIEW.md` - plain-language overview of the Tandem KB MCP and ACA autonomous coder
- `internal/PYTHON_MCP_BLUEPRINT_PROMPT_SPEC.md` - internal prompt-spec for scaffolding reusable Python MCP server blueprints

Internal implementation details live under `docs/internal/` and are intentionally kept out of the public read order.

## Suggested Read Order

1. Read `../AGENTS.md`
2. Read `PORTABLE_AUTOCODER_AGENT_PLAN.md`
3. Read `TASK_SOURCES.md`
4. Read `KANBAN_BOARD.md`
5. Read `REPO_BINDING.md`
6. Read `CODING_TASKS_WITH_TANDEM.md`
7. Read `CONFIG_SCHEMA.md`
8. Read `COMMANDS.md`
9. Read `RUN_STATE_SCHEMA.md`
10. Read `ENGINE_MANAGEMENT.md`
11. Read `DOCKER_COMPOSE.md`
12. Read `MONITORING.md`
13. Read `TANDEM_KB_MCP_AND_ACA_OVERVIEW.md`
14. Run `../scripts/setup.sh` to seed token files and the local control-panel config

## Typical Setup

1. Choose the repo to operate on.
2. Choose the task source or Kanban board.
3. Confirm Tandem engine status and let the launcher reuse a running engine when possible.
4. Choose provider and model.
5. Choose the storage profile (`local` for SQLite, `shared` for the Postgres coordination path).
6. Decide whether swarm mode is enabled.
7. Decide whether to run locally or via Docker Compose.
8. Run the agent with the repo binding and config loaded.
9. Use `scripts/monitor.sh` to inspect the latest run and board snapshots.
10. If the task turns into code edits, follow `CODING_TASKS_WITH_TANDEM.md` for the workspace, diff, and verification loop.

## What This Directory Is Not

- It is not the git repo under edit.
- It is not a secret store.
- It is not a place for generated code.
