# ACA Config Schema

Last updated: 2026-03-21
Status: Draft

## Purpose

This document defines the canonical v0.1 config contract for ACA.

It is the reference point for:

- `config/agent.yaml`
- `.env.example`
- `scripts/run.sh`
- `scripts/entrypoint.sh`
- `docker-compose.yml`

The goal is to keep one logical config model even though the runtime still uses both YAML and environment variables.

## Canonical Rule

- `config/agent.yaml` is the canonical checked-in operator profile for non-secret settings.
- `tandem-data/control-panel-config.json` is the local control-panel-owned install/runtime config for non-secret overrides.
- `.env` is the local secret and bootstrap layer.
- For Tandem auth, prefer `TANDEM_API_TOKEN_FILE` and a mounted secret file; reserve raw token env vars for legacy or manual runs.
- `scripts/run.sh` and `scripts/entrypoint.sh` resolve all of the above into one effective runtime config.
- Docker Compose may inject container-local infrastructure settings such as the workspace root.
- ACA should prefer an already-running Tandem engine at the configured `base_url` and only start a new local engine when the config allows it.
- `TANDEM_ENGINE_RELEASE_VERSION` and `TANDEM_CONTROL_PANEL_RELEASE_VERSION` are build-time pins for the Tandem npm packages used by the container images; when unset, Compose builds use `latest`.
- `TANDEM_RELEASE_VERSION` remains a backwards-compatible build-time pin for both Tandem packages when a package-specific pin is not set.

## Precedence

When the same setting appears in multiple places, use this order:

1. explicit runtime override
2. environment variable from `.env`
3. value in `tandem-data/control-panel-config.json`
4. value in `config/agent.yaml`
5. built-in default

## Control Panel Install Config

The control panel writes durable non-secret install choices to `tandem-data/control-panel-config.json`.

Relevant keys include:

- `control_panel.mode`
- `control_panel.aca_compact_nav`
- `agent`
- `tandem`
- `task_source`
- `repository`
- `provider`
- `storage`
- `review`
- `scheduler`
- `artifact_store`
- `execution`
- `swarm`
- `output`
- `mcp_servers`
- `github_mcp`

The control panel uses this file for repo binding, task source selection, provider/model defaults, swarm policy, and ACA navigation defaults. Environment variables remain available as bootstrap overrides and legacy compatibility inputs.

## Top-Level YAML Schema

### `agent`

Required fields:

- `name`: string, the agent identity label
- `dry_run`: boolean, stop after validation when true

### `tandem`

Required fields:

- `base_url`: string, Tandem engine URL

Optional fields:

- `token_file`: string, path to a mounted secret file that holds the Tandem token; prefer `tandem-data/tandem_api_token` for Compose or `/run/secrets/tandem_api_token` for custom secret mounts
- `token_env`: string, name of the env var that holds the Tandem token as a legacy fallback; prefer `TANDEM_API_TOKEN`
- `required_version`: string, minimum acceptable Tandem engine version
- `startup_mode`: string, one of `reuse_only` or `reuse_or_start`
- `update_policy`: string, one of `notify`, `block`, or `ignore`
- `engine_command`: string, command used to start a local Tandem engine when allowed; defaults to `scripts/tandem-engine-serve.sh`

### `task_source`

Required fields:

- `type`: one of `github_project`, `linear`, `local_backlog`, `manual`, `custom`, `kanban_board`

Variant-specific fields:

- `github_project`: `owner`, `project`, and either `item` or `url`
- `linear`: `team`; optional `project`, `statuses`, `labels`, `query`, and either `item` or `url`
- `local_backlog`: `path`
- `manual`: `prompt`
- `custom`: `source_name`, `payload`
- `kanban_board`: `path`, optional `card_id`

### `repository`

Required fields:

- at least one of `path`, `slug`, or `clone_url`

Optional fields:

- `path`: existing local checkout path
- `slug`: GitHub `owner/repo` slug
- `clone_url`: remote git URL
- `default_branch`: string, defaults to `main`
- `worktree_root`: string, host-side worktree base
- `remote_name`: string, defaults to `origin`

### `provider`

Required fields:

- `id`: provider ID
- `model`: model ID

Optional fields:

- `fallback_provider`
- `fallback_model`

### `storage`

Optional fields:

- `profile`: storage profile, one of `local` or `shared`
- `postgres_url`: Postgres coordination DSN used when `storage.profile` is `shared`

Behavior:

- `local` maps to the current SQLite coordination path used by ACA today
- `shared` maps to the Postgres coordination backend for shared deployments

### `review`

Optional fields:

- `policy`: human handoff policy, one of `human_review` or `auto_merge`
- `auto_merge_strategy`: merge method ACA may use when `policy` is `auto_merge`; one of `merge`, `squash`, or `rebase`
- `auto_merge_allowed_strategies`: comma-separated allow-list of merge methods
- `merge_requires_approval`: boolean, require explicit operator approval before ACA performs the merge; defaults to `true`
- `branch_delete_requires_approval`: boolean, require separate operator approval before ACA deletes the remote branch; defaults to `true`
- `delete_branch_after_merge`: boolean, allow ACA to delete the remote branch after merge when policy and approvals allow it; defaults to `true`

Behavior:

- `human_review` keeps merge approval with a human gate
- `auto_merge` is opt-in and guarded: ACA only finalizes ACA-created branches for the configured repository when checks are successful, review is approved, the lifecycle is `ready-to-merge`, and the configured strategy is allowed
- Merge approval and branch deletion approval are separate decisions. Operators may approve the merge while leaving branch cleanup manual.
- ACA refuses to merge when review or check state cannot be proven clean; successful guarded merges record merge metadata and only delete the remote ACA branch when deletion is enabled and approved or approval is disabled by policy.

### `scheduler`

Optional fields:

- `policy`: scheduler policy, currently `fair_round_robin`
- `max_active_tasks`: maximum total admitted tasks across the workspace
- `max_active_tasks_per_project`: maximum admitted tasks per project binding
- `max_active_tasks_per_repo`: maximum admitted tasks per repo/worktree scope
- `queue_depth_limit`: maximum queued tasks the scheduler will inspect in one pass

Behavior:

- `fair_round_robin` admits tasks across projects in a conservative round-robin order
- repo overlap is serialized by default through the per-repo cap
- the scheduler emits durable admission events into the coordination ledger

### `execution`

Optional fields:

- `backend`: one of `auto`, `legacy`, or `coder`
- `coder_wait_timeout_seconds`: how long ACA should poll a Tandem coder run before reporting that ACA's supervisor wait budget expired; default `3600`
- `coder_poll_interval_seconds`: how often ACA should poll Tandem for coder run status; default `15`
- `coder_supervisor_enabled`: keep reconciling detached Tandem coder runs after ACA request timeout or process restart; default `true`
- `coder_supervisor_interval_seconds`: background reconciliation interval; default `30`
- `coder_supervisor_batch_size`: maximum active coder runs checked per supervisor tick; default `100`
- `coder_cancel_on_source_terminal`: allow ACA to cancel a still-running Tandem coder run when the source task is already terminal; default `true`

Behavior:

- `auto`: prefer Tandem coder for GitHub task/project coding runs when ACA has enough metadata, otherwise fall back to ACA's legacy orchestration path
- `legacy`: keep ACA's current manager/worker/reviewer/tester pipeline
- `coder`: require Tandem coder for supported tasks and fail closed when ACA cannot derive a supported coder workflow
- coder runs are allowed to be long-running; an HTTP timeout from `execute_all` is not treated as terminal while Tandem still has a non-terminal run state
- the coder supervisor is the only ACA component that turns detached coder runs into final GitHub/project outcomes; non-terminal Tandem state keeps ACA `running`

### `swarm`

Required fields:

- `enabled`: boolean
- `shared_model`: boolean
- `max_workers`: integer, at least 1

Optional fields:

- `manager`
- `worker`
- `reviewer`
- `tester`

Role override rule:

- if `shared_model` is true, role-specific overrides must be omitted
- if `shared_model` is false, role-specific overrides may be supplied selectively

### `output`

Optional fields:

- `root`: directory for local run artifacts and summaries

### `artifact_store`

Optional fields:

- `root`: shared artifact-store root used to mirror run outputs and worker artifacts for recovery; when unset, ACA defaults to a local `artifact-store` directory next to the run output root

## Workspace Registry

The multi-project workspace registry is runtime state, not a config field, but it follows the same file-oriented contract as the rest of ACA.

Default layout:

- `.tandem-agents/workspace.yaml`
- `.tandem-agents/projects/<project_id>.yaml`

Behavior:

- the workspace file stores the canonical workspace view and project list
- each project file mirrors one project binding for easier inspection and recovery
- `./scripts/run.sh workspace` prints the current workspace view
- `./scripts/run.sh workspace --set-active <project_id>` updates the active project binding

## Environment Schema

### Tandem

- `TANDEM_BASE_URL` -> `tandem.base_url`
- `TANDEM_PORT` -> Compose-sidecar port and engine wrapper port; default `39733` in Docker, override if another service already uses it
- `TANDEM_API_TOKEN_FILE` -> `tandem.token_file`
- `TANDEM_API_TOKEN` -> Tandem auth secret used only as a legacy fallback
- `TANDEM_TOKEN` -> legacy Tandem auth alias
- `TANDEM_REQUIRED_VERSION` -> `tandem.required_version`
- `TANDEM_STARTUP_MODE` -> `tandem.startup_mode`
- `TANDEM_UPDATE_POLICY` -> `tandem.update_policy`
- `TANDEM_ENGINE_COMMAND` -> `tandem.engine_command`
- `TANDEM_ENGINE_PACKAGE` -> build-time engine npm package for engine and ACA container images; defaults to `@frumu/tandem`; hosted/managed builds can set `@frumu/tandem-enterprise`
- `TANDEM_ENGINE_RELEASE_VERSION` -> build-time engine package version pin for engine and ACA container images; when unset, Compose builds use `latest`
- `TANDEM_CONTROL_PANEL_RELEASE_VERSION` -> build-time `@frumu/tandem-panel` package pin for the control panel container image; when unset, Compose builds use `latest`
- `TANDEM_RELEASE_VERSION` -> backwards-compatible build-time pin used for both package-specific pins when they are unset

### Control Panel

- `TANDEM_CONTROL_PANEL_MODE` -> `control_panel.mode`
- `TANDEM_CONTROL_PANEL_CONFIG_FILE` -> local control-panel config path
- `TANDEM_CONTROL_PANEL_STATE_DIR` -> state directory for browser-side and control-panel runtime files
- `TANDEM_CONTROL_PANEL_AUTO_START_ENGINE` -> whether the control panel may start a Tandem engine automatically
- `TANDEM_CONTROL_PANEL_PUBLIC_URL` -> optional externally visible control-panel URL, usually the hosted deployment subdomain

### Identity

- `AGENT_NAME` -> `agent.name`

### Task Source

- `ACA_TASK_SOURCE_TYPE` -> `task_source.type`
- `ACA_TASK_SOURCE_OWNER` -> `task_source.owner`
- `ACA_TASK_SOURCE_REPO` -> `task_source.repo`
- `ACA_TASK_SOURCE_TEAM` -> `task_source.team`
- `ACA_TASK_SOURCE_PROJECT` -> `task_source.project`
- `ACA_TASK_SOURCE_STATUSES` -> `task_source.statuses`
- `ACA_TASK_SOURCE_LABELS` -> `task_source.labels`
- `ACA_TASK_SOURCE_QUERY` -> `task_source.query`
- `ACA_TASK_SOURCE_ITEM` -> `task_source.item`
- `ACA_TASK_SOURCE_URL` -> `task_source.url`
- `ACA_TASK_SOURCE_PATH` -> `task_source.path`
- `ACA_TASK_SOURCE_CARD_ID` -> `task_source.card_id`
- `AUTOCODER_TASK_SOURCE_*` -> legacy aliases still accepted by the loader

### Repository Binding

- `ACA_REPO_PATH` -> `repository.path`
- `ACA_REPO_SLUG` -> `repository.slug`
- `ACA_REPO_URL` -> `repository.clone_url`
- `ACA_DEFAULT_BRANCH` -> `repository.default_branch`
- `ACA_WORKTREE_ROOT` -> `repository.worktree_root`
- `ACA_WORKSPACE_ROOT` -> container workspace base used by Docker Compose
- `ACA_REMOTE_NAME` -> `repository.remote_name`
- `AUTOCODER_REPO_*` and related branch/remote names -> legacy aliases still accepted by the loader

### Provider / Model

- `ACA_PROVIDER` -> `provider.id`
- `ACA_MODEL` -> `provider.model`
- `ACA_EXECUTION_BACKEND` -> `execution.backend`
- `ACA_CODER_WAIT_TIMEOUT_SECONDS` -> `execution.coder_wait_timeout_seconds`
- `ACA_CODER_POLL_INTERVAL_SECONDS` -> `execution.coder_poll_interval_seconds`
- `ACA_CODER_SUPERVISOR_ENABLED` -> `execution.coder_supervisor_enabled`
- `ACA_CODER_SUPERVISOR_INTERVAL_SECONDS` -> `execution.coder_supervisor_interval_seconds`
- `ACA_CODER_SUPERVISOR_BATCH_SIZE` -> `execution.coder_supervisor_batch_size`
- `ACA_CODER_CANCEL_ON_SOURCE_TERMINAL` -> `execution.coder_cancel_on_source_terminal`
- `ACA_PROVIDER_KEY` -> primary generic provider secret for simple single-provider setups; ACA maps it onto the active provider's expected secret env var for Tandem subprocesses
- `ACA_FALLBACK_PROVIDER` -> `provider.fallback_provider`
- `ACA_REVIEW_POLICY` -> `review.policy`
- `ACA_MERGE_REQUIRES_APPROVAL` -> `review.merge_requires_approval`
- `ACA_BRANCH_DELETE_REQUIRES_APPROVAL` -> `review.branch_delete_requires_approval`
- `ACA_DELETE_BRANCH_AFTER_MERGE` -> `review.delete_branch_after_merge`
- `ACA_FALLBACK_MODEL` -> `provider.fallback_model`
- `AUTOCODER_PROVIDER` and friends -> legacy aliases still accepted by the loader

### Storage

- `ACA_STORAGE_PROFILE` -> `storage.profile`
- `ACA_COORDINATION_POSTGRES_URL` -> `storage.postgres_url`

### Scheduler

- `ACA_SCHEDULER_POLICY` -> `scheduler.policy`
- `ACA_SCHEDULER_MAX_ACTIVE_TASKS` -> `scheduler.max_active_tasks`
- `ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT` -> `scheduler.max_active_tasks_per_project`
- `ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO` -> `scheduler.max_active_tasks_per_repo`
- `ACA_SCHEDULER_QUEUE_DEPTH_LIMIT` -> `scheduler.queue_depth_limit`

### Artifact Store

- `ACA_ARTIFACT_STORE_ROOT` -> `artifact_store.root`

### Provider Secrets

- `OPENAI_API_KEY` -> preferred secret for `openai`, including OpenAI-compatible custom `ACA_PROVIDER_BASE_URL` targets such as MiniMax
- `OPENROUTER_API_KEY` -> preferred secret for `openrouter`
- `ANTHROPIC_API_KEY` -> preferred secret for `anthropic`
- `GROQ_API_KEY` -> preferred secret for `groq`
- `MISTRAL_API_KEY` -> preferred secret for `mistral`
- `TOGETHER_API_KEY` -> preferred secret for `together`
- `COHERE_API_KEY` -> preferred secret for `cohere`

ACA passes `.env` secrets through to Tandem subprocesses for local runs and Docker Compose runs.
For simple single-provider setups, prefer `ACA_PROVIDER_KEY`.
If a provider-specific secret env var is missing, ACA will populate the matching provider env var automatically from `ACA_PROVIDER_KEY`.

### Swarm

- `ACA_ENABLE_SWARM` -> `swarm.enabled`
- `ACA_SHARED_MODEL` -> `swarm.shared_model`
- `ACA_MAX_WORKERS` -> `swarm.max_workers`
- `ACA_MANAGER_PROVIDER` -> `swarm.manager.provider`
- `ACA_MANAGER_MODEL` -> `swarm.manager.model`
- `ACA_WORKER_PROVIDER` -> `swarm.worker.provider`
- `ACA_WORKER_MODEL` -> `swarm.worker.model`
- `ACA_REVIEWER_PROVIDER` -> `swarm.reviewer.provider`
- `ACA_REVIEWER_MODEL` -> `swarm.reviewer.model`
- `ACA_TESTER_PROVIDER` -> `swarm.tester.provider`
- `ACA_TESTER_MODEL` -> `swarm.tester.model`
- `AUTOCODER_*` swarm env vars -> legacy aliases still accepted by the loader

### Output / Traceability

- `ACA_OUTPUT_ROOT` -> `output.root`
- `ACA_DRY_RUN` -> `agent.dry_run`
- `AUTOCODER_OUTPUT_ROOT` and `AUTOCODER_DRY_RUN` -> legacy aliases still accepted by the loader

### GitHub Access

- `GITHUB_PERSONAL_ACCESS_TOKEN` -> preferred GitHub PAT for Tandem's built-in GitHub MCP path
- `GITHUB_TOKEN` -> fallback GitHub PAT for Tandem's built-in GitHub MCP path
- `GITHUB_PERSONAL_ACCESS_TOKEN_FILE` -> preferred mounted secret file for Tandem's built-in GitHub MCP path
- `GITHUB_TOKEN_FILE` -> fallback mounted secret file for Tandem's built-in GitHub MCP path
- `ACA_GITHUB_MCP_ENABLED` -> when `true`, ACA prepares the persisted `github` MCP entry on engine boot
- `ACA_GITHUB_MCP_URL` -> transport URL ACA should enforce for the persisted `github` MCP entry
- `ACA_GITHUB_MCP_TOOLSETS` -> GitHub MCP toolsets ACA should persist, for example `default,projects`
- `ACA_GITHUB_MCP_SCOPE` -> ACA runtime policy for when GitHub MCP should stay connected: `none`, `intake_only`, `intake_finalize`, or `always`
- `ACA_KB_MCP_ENABLED` -> when `true`, ACA prepares the persisted `kb` MCP entry on engine boot
- `ACA_GITHUB_REMOTE_SYNC` -> finalize-time GitHub write policy for GitHub Project tasks: `off`, `status`, or `status_comment`

### Linear Access

- `ACA_LINEAR_MCP_ENABLED` -> when `true`, ACA expects a Linear MCP server in the Tandem engine registry
- `ACA_LINEAR_MCP_SERVER` -> server name to use for Linear MCP calls; default `linear`
- `ACA_LINEAR_MCP_SCOPE` -> runtime policy for when Linear MCP should stay connected: `none`, `intake_only`, `intake_finalize`, or `always`
- `ACA_LINEAR_REMOTE_SYNC` -> finalize-time Linear write policy for Linear tasks: `off`, `status`, `status_comment`, or `rich`
- `ACA_LINEAR_CLAIM_LABEL` -> label ACA adds while claiming/running a Linear issue
- `ACA_LINEAR_DONE_LABEL` -> label ACA adds when a run completes
- `ACA_LINEAR_BLOCKED_LABEL` -> label ACA adds when a run blocks/fails
- `ACA_LINEAR_CLAIM_STATUS` -> Linear status to set after claim; default `In Progress`
- `ACA_LINEAR_REVIEW_STATUS` -> Linear status to set on completed runs; default `In Review`
- `ACA_LINEAR_DONE_STATUS` -> reserved done status mapping; default `Done`
- `ACA_LINEAR_BLOCKED_STATUS` -> Linear status to set on blocked/failed runs; default `Blocked`

Linear OAuth and workspace authorization live in the Tandem control panel MCP connection. Do not put Linear API tokens in ACA config.

## Validation Rules

- the agent must reject runs without a task source
- the agent must reject runs without a repository binding
- the agent must reject runs without a provider/model selection
- the agent must reject invalid task source combinations
- the agent must reject `kanban_board` sources without a board path
- the agent must reject `linear` sources without a team
- the agent must reject role-specific swarm overrides when `shared_model` is true
- the agent must prefer a running Tandem engine when it is reachable
- the agent must notify when the running Tandem engine is older than the configured requirement
- the agent must not downgrade or restart a newer compatible Tandem engine
- the agent must stop early when `dry_run` is true

## Minimal Example

### `config/agent.yaml`

```yaml
agent:
  name: ACA
  dry_run: false

tandem:
  base_url: http://localhost:39733
  token_file: /run/secrets/tandem_api_token
  token_env: TANDEM_API_TOKEN
  required_version: ">=0.4.8"
  startup_mode: reuse_or_start
  update_policy: notify
  engine_command: scripts/tandem-engine-serve.sh

task_source:
  type: kanban_board
  path: config/board.yaml
  card_id: card-123

repository:
  path: /home/user/projects/my-app
  default_branch: main
  worktree_root: /home/user/worktrees
  remote_name: origin

provider:
  id: openai
  model: gpt-4.1-mini

swarm:
  enabled: true
  shared_model: false
  max_workers: 3

output:
  root: runs
```

### `.env`

```bash
TANDEM_BASE_URL=
TANDEM_PORT=39733
TANDEM_API_TOKEN_FILE=
TANDEM_API_TOKEN=
TANDEM_REQUIRED_VERSION=>=0.4.8
TANDEM_STARTUP_MODE=reuse_or_start
TANDEM_UPDATE_POLICY=notify
TANDEM_ENGINE_COMMAND=scripts/tandem-engine-serve.sh
ACA_TASK_SOURCE_TYPE=kanban_board
ACA_TASK_SOURCE_PATH=config/board.yaml
ACA_TASK_SOURCE_CARD_ID=
ACA_REPO_PATH=/home/user/projects/my-app
ACA_OUTPUT_ROOT=runs
ACA_ENABLE_SWARM=true
ACA_MAX_WORKERS=3
```

## Current Status

The launcher and Docker draft already read environment variables directly and now support a mounted secret file for the Tandem token.
This document defines the target contract so the next implementation step can keep YAML, env, and the secret file aligned in one runtime config.
