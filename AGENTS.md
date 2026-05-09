# ACA Agent Instructions

This directory is the working home for a portable ACA agent.

## Read First

1. `docs/README.md`
2. `docs/PORTABLE_AUTOCODER_AGENT_PLAN.md`
3. `docs/TASK_SOURCES.md`
4. `docs/KANBAN_BOARD.md`
5. `docs/REPO_BINDING.md`
6. `docs/CONFIG_SCHEMA.md`
7. `docs/ENGINE_MANAGEMENT.md`
8. `docs/COMMANDS.md`
9. `docs/RUN_STATE_SCHEMA.md`
10. `.env.example`

## Purpose

Use this directory as the local control plane for running a repo-agnostic ACA.

The agent should:

- take work from a task source
- bind to a git repository
- choose a provider and model
- optionally fan out to a small swarm
- write back a summary, artifacts, and safe handoff notes

## Rules

- Do not commit secrets or tokens.
- Do not edit files outside the intended repository unless the task explicitly requires it.
- Do not assume the workspace is the repo.
- Do not start coding until the task source, repo binding, and execution backend status are clear.
- Prefer the smallest swarm that can safely finish the task.
- Keep public-facing comments short and safe.
- Keep changes scoped to the active task.
- Treat the Kanban board as the durable task queue and the run blackboard as the coordination record for one run.
- Treat the multi-project plan as the reference shape for fan-out across many repos and many tasks.
- Do not pick up Tandem backend or control-panel implementation phases from the ACA plan; those belong in the Tandem repo.
- Update `docs/README.md` when moving, adding, or renaming docs.
- Use commands from `docs/COMMANDS.md` for inspection and triggers when possible.

## Developer Standards

- Prefer small, focused files. Aim for roughly 200 to 300 lines per file and split files that grow past about 400 lines unless there is a strong reason not to.
- Keep documentation modular. Use one index doc plus smaller topic docs instead of one oversized reference.
- Keep backend execution details in `docs/internal/` instead of the public planning docs.
- Keep shell scripts simple. If branching or orchestration logic grows, move it into a dedicated helper or module.
- Keep runtime artifacts out of tracked docs. Generated files belong in `runs/` or another ignored runtime directory.
- Prefer explicit config, named fields, and fail-closed validation over hidden defaults.
- Avoid unrelated cleanup or broad formatting changes. Keep the patch tightly scoped.
- Use ASCII by default unless a file already uses Unicode or the content requires otherwise.

## Backend Guidance

- Keep remote task-source and execution-backend details out of the public planning docs.
- Keep `git` for repository checkout, worktrees, branches, commits, and diffs.
- If you are working on backend integration details, read `docs/internal/TANDEM_INTEGRATION_PLAN.md` first.

## Configuration

- Prefer `./scripts/setup.sh` for first-run `.env` creation and updates.
- Copy `.env.example` to `.env` manually only when you need full direct control.
- Treat `.env` as local only.
- Keep provider/model settings explicit and auditable.
- Keep the repository default branch explicit in config so clone and worktree behavior stay predictable.

## Output Expectations

Each run should leave behind:

- a short summary of what happened
- any artifact paths
- the selected execution backend version/status
- the selected provider/model
- the selected task source
- the selected board path/card id, if the task came from a board
- the blackboard path for the current run
- the repo binding that was used
