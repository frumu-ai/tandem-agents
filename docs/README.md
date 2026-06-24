# Tandem Agents Docs

This directory contains the setup, schema, and workflow docs for the local
Tandem Agents stack: Tandem engine, control panel, ACA runtime, KB MCP, and
supporting operator scripts.

ACA smoke harness documentation:

- [ACA_SMOKE_HARNESS.md](ACA_SMOKE_HARNESS.md) - contract for the ACA smoke
  harness purpose, expected task order, docs-only file scope, and verification
  command.

Project files live one level up in the repository tree:

- [../README.md](../README.md) - root pointer to this docs index
- [../AGENTS.md](../AGENTS.md) - agent instructions
- [../RELEASE_NOTES.md](../RELEASE_NOTES.md) - release notes for ACA runtime changes
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
- [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) - dependency notices and asset/trademark boundaries
- [../scripts/run.sh](../scripts/run.sh) - local launcher stub
- [../scripts/setup.sh](../scripts/setup.sh) - bootstrap and migration helper for `.env` and control-panel config
- [../scripts/build-containers.sh](../scripts/build-containers.sh) - Compose build helper that resolves the latest Tandem release by default
- [../scripts/monitor.sh](../scripts/monitor.sh) - local run inspector
- [../scripts/tandem-engine-serve.sh](../scripts/tandem-engine-serve.sh) - Tandem sidecar wrapper that reads the token file
- [../docker-compose.yml](../docker-compose.yml) - containerized runtime draft
- [../docker-compose.published.yml](../docker-compose.published.yml) - Compose stack that pulls published GHCR images
- [IMAGE_PUBLISHING.md](IMAGE_PUBLISHING.md) - GHCR image publish flow for public and hosted enterprise images
- [../config/Dockerfile](../config/Dockerfile) - container image draft
- [../config/Dockerfile.engine](../config/Dockerfile.engine) - engine-only sidecar image
- [../.dockerignore](../.dockerignore) - Docker build context exclusions
- [../scripts/entrypoint.sh](../scripts/entrypoint.sh) - clone-and-prepare flow for containers
- [../.env.example](../.env.example) - local configuration template
- [../secrets/](../secrets/) - local mounted secret files, gitignored

## Files

- `PORTABLE_AUTOCODER_AGENT_PLAN.md` - design note for the portable auto-coder
- `LOCAL_QUICKSTART.md` - first-time local Docker Compose setup and run guide
- `TASK_SOURCES.md` - task source contract and examples
- `KANBAN_BOARD.md` - board, blackboard, and run coordination contract
- `REPO_BINDING.md` - repo selection and worktree guidance
- `CONFIG_SCHEMA.md` - canonical config contract for YAML and env
- `ENGINE_MANAGEMENT.md` - Tandem engine reuse, start, and update policy
- `COMMANDS.md` - operator commands for checking and triggering actions
- `RUN_STATE_SCHEMA.md` - exact run-state contract for status and events
- `CODING_TASKS_WITH_TANDEM.md` - coding-task execution loop for Tandem-backed repository edits
- `CODING_TASK_CONTRACT.md` - canonical coding-task envelope, coder handoff, and debugger surface contract
- `DOCKER_COMPOSE.md` - containerized startup notes
- `MONITORING.md` - how to watch runs and logs
- `TANDEM_CONTROL_PANEL_INTEGRATION.md` - how to connect external control panels
- `LOCAL_KANBAN_TEST_GUIDE.md` - practical Docker-based test flow using a local board and a small repo
- `LINEAR_ACA_SMOKE_RUNBOOK.md` - mocked and live-readiness flow for Linear-to-PR ACA smoke testing
- `kb_mcp.md` - KB MCP agent-guide upgrade plan and kanban board
- `TANDEM_KB_MCP_AND_ACA_OVERVIEW.md` - plain-language overview of the Tandem KB MCP and ACA autonomous coder

## Suggested Read Order

1. Read `../AGENTS.md`
2. Read `LOCAL_QUICKSTART.md`
3. Read `PORTABLE_AUTOCODER_AGENT_PLAN.md`
4. Read `TASK_SOURCES.md`
5. Read `KANBAN_BOARD.md`
6. Read `REPO_BINDING.md`
7. Read `CODING_TASKS_WITH_TANDEM.md`
8. Read `CODING_TASK_CONTRACT.md`
9. Read `CONFIG_SCHEMA.md`
10. Read `COMMANDS.md`
11. Read `RUN_STATE_SCHEMA.md`
12. Read `ENGINE_MANAGEMENT.md`
13. Read `DOCKER_COMPOSE.md`
14. Read `MONITORING.md`
15. Read `TANDEM_KB_MCP_AND_ACA_OVERVIEW.md`
16. Read `LINEAR_ACA_SMOKE_RUNBOOK.md` before testing the Linear ACA coding loop
17. Run `../scripts/setup.sh` to seed token files and the local control-panel config

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
