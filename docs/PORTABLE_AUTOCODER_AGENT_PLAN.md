# Portable Auto Coder Agent Plan

Last updated: 2026-03-21
Status: Draft
Owner: Engine / Coder

## Purpose

This document sketches a repo-agnostic auto coder agent that can:

- pull work from a GitHub Project, issue list, or other task source
- bind itself to any git repository, not just the ROS project
- choose an LLM provider and model explicitly
- coordinate a small agent swarm for decomposition, implementation, review, and test
- write back progress, artifacts, and final results safely

The starting point for this idea is the ROS-specific workflow in:

- `~/ros/agents/GITHUB/GITHUB_PROJECT_AGENT_WORKFLOW.md`

That workflow is useful as a behavioral template, but it is too repo-specific to serve as the final architecture. This plan generalizes the workflow so it can run anywhere Tandem can bind a repository and a task source.

## Repo-Grounded Inputs

The repo already has the main pieces we need:

- Python SDK package: `packages/tandem-client-py`
- provider catalog and provider config models
- coder, agent team, mission, automation, and workflow plan SDK namespaces
- swarm-oriented examples in `examples/agent-swarm`
- provider/model setup flows in the quickstart portal

Relevant Python SDK entry points already exposed by `tandem-client`:

- `client.providers`
- `client.coder`
- `client.agent_teams`
- `client.missions`
- `client.automations_v2`
- `client.workflow_plans`

The SDK package already ships the models needed for this kind of agent:

- `ProviderCatalog`
- `ProvidersConfigResponse`
- `ProviderEntry`
- `ProviderModelEntry`
- `CoderRunRecord`
- `CoderRepoBinding`
- `CoderGithubRef`
- `AgentTeamTemplate`
- `MissionRecord`
- `WorkflowPlan`

## Core Idea

The agent should not hardcode one workflow like "GitHub Project issue triage for ROS".

Instead, it should be built as three layers:

1. Task intake
2. Repo/workspace binding
3. Execution orchestration

That separation makes the same agent usable for:

- GitHub Project items
- GitHub issues or PRs
- local backlog files
- future task adapters

## Proposed Architecture

### 1. Task Intake Layer

This layer resolves a task from a source into a normalized work item.

Inputs may include:

- GitHub Project item
- GitHub issue
- GitHub PR review request
- local markdown backlog item
- manual prompt from a user

Normalized task fields should include:

- source type
- source id
- title
- description
- acceptance criteria
- repository binding
- priority
- labels or tags
- links to upstream evidence

### 2. Repo Binding Layer

This layer decides where the agent is allowed to work.

It should support:

- an explicit git repository path
- a remote repo slug
- an inferred workspace root
- a default branch
- a worktree per task or per swarm worker

This layer must fail closed if the repository is not available or the workspace is ambiguous.

### 3. Execution Orchestration Layer

This layer turns the task into actions.

Recommended stages:

1. ingest task
2. inspect repository
3. plan the change
4. fan out work to worker agents
5. run tests or validation
6. repair failures if needed
7. write the final artifact or patch
8. commit, push, or hand off

## Provider And Model Selection

The agent should let the operator choose:

- provider
- model
- fallback model, if supported
- whether the selection is shared across the whole swarm or unique per role

The simplest useful policy is:

- one provider/model for the manager/orchestrator
- one provider/model for worker agents
- one provider/model for reviewer/tester agents

This keeps costs and latency predictable while still allowing specialization.

### Suggested policy shape

```json
{
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "swarm_policy": {
    "shared_model": false,
    "manager": { "provider": "openai", "model": "gpt-4.1" },
    "worker": { "provider": "openrouter", "model": "anthropic/claude-sonnet-4" },
    "reviewer": { "provider": "openai", "model": "gpt-4.1-mini" },
    "tester": { "provider": "openai", "model": "gpt-4.1-mini" }
  }
}
```

### Selection rules

- Provider choice should come from the live provider catalog, not from a hardcoded allowlist in the agent logic.
- Model choice should be constrained by the selected provider and current availability.
- If the selected model is missing, the agent should prompt for a replacement or downgrade to a configured fallback.
- The final selection should be persisted with the run so the exact execution path is auditable.

## Swarm Model

The agent should support small, bounded swarms rather than unconstrained parallelism.

Recommended roles:

- `manager` - interprets the task, decomposes work, and owns the overall run
- `worker` - implements concrete file changes
- `reviewer` - checks correctness and design fit
- `tester` - runs validations and reports failures

Recommended swarm rules:

- workers should operate in isolated worktrees
- each worker should own a disjoint slice of the task
- the manager should own task decomposition and merge decisions
- reviewer/tester should not edit production files unless asked to repair
- concurrency should be capped by a mission-level setting

### Good swarm use cases

- large repo-wide refactors
- multi-file bug fixes
- feature work with separate frontend/backend slices
- changes that benefit from independent review and test agents

### Bad swarm use cases

- single-file edits
- tiny typo fixes
- tasks with unclear acceptance criteria
- tasks that already fit in one agent context window

## Execution Flow

1. Resolve the task source.
2. Check Tandem engine status and reuse or start it according to policy.
3. Bind the repository and workspace.
4. Choose provider and model.
5. Create a run record with source metadata.
6. Ask the manager agent to produce a plan.
7. Spawn worker agents for parallel implementation slices.
8. Run reviewer and tester agents on the outputs.
9. Repair failures or ask for human input when blocked.
10. Write the final patch, summary, and task update.
11. Record artifacts and close the run.

## Integration Points In Tandem

The portable agent should build on the existing Tandem surfaces instead of inventing a second orchestration stack.

Suggested SDK usage:

- `TandemClient` for engine access
- `client.providers` to list/configure provider state
- `client.coder` for coder runs, artifacts, and memory hits
- `client.agent_teams` for swarm roles and templates
- `client.workflow_plans` for task decomposition previews
- `client.missions` for higher-level mission tracking when the work is multi-step

This means the portable agent can be implemented as an orchestration layer over the existing engine, not as a separate runtime.

## Safety And Guardrails

The agent should default to conservative behavior:

- do not assume write access until the repo binding is validated
- do not hardcode secrets, tokens, or private issue content into tracked files
- do not post sensitive diagnostics to public GitHub comments
- do not move a task to done until the repo state is consistent
- do not fan out more workers than the task actually needs
- do not continue if the provider/model selection is invalid

For GitHub-backed tasks, the agent should preserve the same public-comment hygiene used by the ROS workflow:

- keep comments short
- summarize what changed
- mention known risks
- avoid private data

## Recommended Configuration Surface

The operator should be able to set:

- Tandem engine base URL
- Tandem engine minimum version
- Tandem engine startup mode
- Tandem engine update policy
- Tandem engine startup command
- task source type
- repository path or slug
- default branch
- provider
- model
- worker count
- reviewer count
- tester count
- shared-model mode on/off
- max parallel workers
- worktree base directory
- output location for run artifacts

Example:

```json
{
  "tandem": {
    "base_url": "http://localhost:39733",
    "required_version": ">=0.4.8",
    "startup_mode": "reuse_or_start",
    "update_policy": "notify",
    "engine_command": "tandem-engine serve --hostname 127.0.0.1 --port 39733"
  },
  "task_source": {
    "type": "github_project",
    "owner": "acme",
    "project": 7
  },
  "repo": {
    "path": "/home/user/projects/my-repo",
    "default_branch": "main"
  },
  "model": {
    "provider": "openai",
    "id": "gpt-4.1-mini"
  },
  "swarm": {
    "enabled": true,
    "workers": 3,
    "reviewers": 1,
    "testers": 1,
    "shared_model": false
  }
}
```

## Implementation Notes

- Keep Tandem engine reuse and version checks in startup helpers, not prompts.
- Keep repo-specific GitHub Project logic in adapters, not in the agent core.
- Keep provider/model logic in configuration and selection helpers, not in prompts.
- Keep swarm coordination in a dedicated orchestration layer so simple single-agent runs stay lightweight.
- Reuse the current coder and mission abstractions instead of creating a separate one-off "autocoder" runtime.

## Next Steps

1. Define a task-source adapter interface.
2. Define a repo-binding interface.
3. Define a provider/model resolution helper.
4. Define a swarm role contract.
5. Add a thin run record that captures source, repo, provider, model, and swarm settings.
6. Build the first portable implementation on top of the current Python SDK.
