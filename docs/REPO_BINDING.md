# Repo Binding

This document explains how the portable auto-coder should decide what repository it is allowed to modify.

In ACA installs, repo binding is usually stored in `tandem-data/control-panel-config.json` and
edited from the control panel Install settings. The `ACA_REPO_*` env vars remain valid bootstrap
and override inputs, but they are no longer the primary place to set repo binding long-term.

## Rule

The agent must always have an explicit repository binding before it edits files.

The binding should be one of:

- local repository path
- remote repository slug
- clone URL plus local checkout path

## Recommended Priority

1. `ACA_REPO_PATH`
2. `ACA_REPO_SLUG`
3. `clone_url` plus `worktree_root`

If the agent cannot choose a repository unambiguously, it should stop and ask for clarification.

## Local Path Example

Use this when the repo already exists on disk:

```text
ACA_REPO_PATH=/home/user/projects/my-app
ACA_DEFAULT_BRANCH=main
```

## GitHub Slug Example

Use this when the agent needs to map from an owner/repo pair:

```text
ACA_REPO_SLUG=acme/my-app
ACA_DEFAULT_BRANCH=main
ACA_WORKTREE_ROOT=/home/user/worktrees
```

## Clone URL Example

Use this when the repo needs to be fetched first:

```text
ACA_REPO_PATH=/home/user/worktrees/my-app
ACA_REPO_SLUG=acme/my-app
ACA_REPO_URL=https://github.com/acme/my-app.git
ACA_REMOTE_NAME=origin
```

If `ACA_REPO_PATH` points at an empty directory and a clone source is configured, ACA will clone into that explicit path instead of inventing a separate checkout.

## Worktree Guidance

- Prefer one worktree per task when running a swarm.
- Keep worktrees under a dedicated base directory.
- Do not let different tasks share the same mutable checkout unless the task is very small.
- In Docker, the clone base is `ACA_WORKSPACE_ROOT`; on the host, use `ACA_WORKTREE_ROOT` for local worktrees.

## Managed Checkout Sync

ACA treats project checkouts under `workspace/repos/...` or `/workspace/repos/...` as
its own managed cache. Before a run starts, ACA fetches the configured remote and
makes the default branch match the remote default branch. If the managed default
branch contains local-only commits, ACA preserves the old tip as a local branch
under `aca/archive/<default-branch>/...` and resets the default branch to the
remote. This lets Linear queue runs continue from GitHub `main` instead of
reusing a dirty local base.

Explicit local repository paths outside the managed cache remain fail-closed:
ACA refuses to reset local-only commits there. Uncommitted changes always block
sync because there is no commit object to archive safely.

## Validation Checklist

Before starting work, verify:

- the repository path exists
- the repo is a git checkout
- the default branch is known
- the worktree root is writable
- the task source points to the same repo or a clearly related repo
