# Coding Tasks With Tandem

This guide explains how ACA should handle code-changing work when it is using Tandem as the execution runtime.

Use this when a task turns into a real repository edit, not just planning or status tracking.
For the canonical task envelope, coder handoff fields, and debugger surface
expectations, see [ACA Coding Task Contract](./CODING_TASK_CONTRACT.md).

## Core rule

Treat Tandem as the execution authority and ACA as the orchestration layer.

- Tandem owns the run state, approvals, and workspace binding.
- The bound repository and worktree are the source of truth for code state.
- Local git handles branches, worktrees, diffs, commits, and cleanup.
- Tandem file tools handle inspection and content edits inside the allowed workspace.

Do not split authority between the ACA app and the Tandem run.

## Before editing

Before an agent edits files, it should confirm:

1. the repository binding is explicit
2. the workspace root is writable
3. the allowed paths match the task
4. the task is in the right worktree or repo checkout

If the binding is ambiguous, stop and ask for clarification instead of guessing.

## Worktrees

Use worktrees for branch-isolated coding work when the task is non-trivial or may run in parallel with other tasks.

- Prefer one worktree per task.
- Keep worktrees under a dedicated base directory.
- Do not let unrelated tasks share the same mutable checkout.
- Let Tandem manage the workspace lifecycle when the coder flow already supports it.

If the task only needs a small local fix and the repo is already isolated, a dedicated worktree may not be necessary.

## Tool choice

Use the smallest tool that makes the change clearly.

- `read` for inspection and context gathering
- `glob` and `grep` for locating files
- `edit` for targeted replacements in an existing file
- `write` for new files or a full-file rewrite
- `apply_patch` for reviewable multi-hunk changes
- `bash` for git status, diff, test, and commit commands

A good default is: inspect first, make the smallest edit possible, then review the diff.

## Recommended coding loop

1. Confirm the repo binding and workspace root.
2. Read the relevant files before changing anything.
3. Decide whether the task is a narrow edit, a rewrite, or a patch.
4. Make the change inside the active worktree or bound workspace.
5. Run the smallest meaningful verification command.
6. Inspect the diff before declaring success.
7. Summarize the files changed, tests run, and any remaining risks.
8. Commit or hand off only after the change is defensible.

## Diff and review

Agents should review changes before closing the loop.

- Use `git diff` or Tandem's diff inspection tools to review the current workspace diff.
- If the diff contains unrelated changes, stop and separate them before continuing.
- If verification fails, fix the local issue instead of hiding it behind a generic success message.

## Testing and verification

Run the smallest test that proves the change is real.

- Prefer a focused unit or integration test before a broad suite.
- If the change touches workflow or runtime boundaries, verify the affected path end to end.
- If the change affects file handling, confirm the file landed where the workflow expected it.
- If the change affects code generation, confirm the generated artifact is the one the run asked for.

A coding task is not done until the agent can say what was verified and what was not.

## How to describe coding tasks

When ACA authors a Tandem prompt or workflow step for code editing, include:

- the workspace root
- any allowed or denied paths
- the files or subsystem being changed
- the expected output or artifact
- the verification command or check
- the review requirement, if any

Example stage contract:

```json
{
  "objective": "Implement the new tenant-scoped audit envelope",
  "workspace_root": "/path/to/repo",
  "allowed_paths": ["crates/tandem-server/src/"],
  "expected_outputs": ["updated Rust source", "focused test result", "diff summary"],
  "verification": "cargo test -p tandem-server provider_auth_set_writes_protected_audit_record -- --nocapture"
}
```

## What not to do

- Do not edit without first confirming the repository binding.
- Do not use a wide rewrite when a narrow edit or patch is enough.
- Do not move between worktrees mid-task unless the run explicitly requires it.
- Do not commit before reviewing the diff and running verification.
- Do not present a code change as complete without saying which files changed.

## Related docs

- [ACA Coding Task Contract](./CODING_TASK_CONTRACT.md)
- [Repo Binding](./REPO_BINDING.md)
- GitHub Projects guidance is kept in internal docs and omitted from the public repo.
- [Autonomous Coding Over Python SDK: Git Access](./AUTONOMOUS_CODING_PYTHON_SDK_GIT_ACCESS.md)
- [Task Sources](./TASK_SOURCES.md)
- [Run State Schema](./RUN_STATE_SCHEMA.md)
