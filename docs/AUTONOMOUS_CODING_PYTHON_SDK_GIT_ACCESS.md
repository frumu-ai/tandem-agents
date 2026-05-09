# Autonomous Coding Over Python SDK: Git Access

This note is for external agents or apps that are setting up autonomous coding on top of `tandem-engine` through the Python SDK.

## Core Rule

Do not build a parallel git executor outside the engine.

If the agent is running through Tandem, Tandem should remain the execution authority for:

- workspace binding
- tool policy
- approvals
- run state
- worktree management
- session history

The external Python app should create sessions or coder runs and let the in-engine agent use Tandem's built-in tools.

## What "git access" actually means in Tandem

There is not a separate first-class git RPC surface in the Python SDK.

In practice, git access comes from the normal local tool surface inside a repo-bound session:

- `bash` for `git status`, `git diff`, `git checkout`, `git commit`, test commands, and other shell work
- `read`
- `glob`
- `grep`
- `write`
- `edit`
- `apply_patch`

So when an autonomous coding agent "uses git" through Tandem, it is usually doing that through the built-in `bash` tool while its session is rooted in the target repository or managed worktree.

For the concrete edit/diff/worktree loop, see [CODING_TASKS_WITH_TANDEM.md](./CODING_TASKS_WITH_TANDEM.md).

## What the external app should not do

Avoid this pattern:

- the Python app manually shells out to `git`
- the Python app tries to maintain its own branch/worktree state machine
- the Python app edits files directly and only uses Tandem for chat

That splits authority between the app and the engine and makes approvals, artifacts, diffs, and run recovery harder to reason about.

## Recommended paths

Use one of these two patterns.

### 1. Session path for a repo agent

Use this when you want a coding agent attached to a repository and you want the model to inspect, edit, and run commands inside that repo.

Flow:

1. Create a session with `directory` set to the repo root.
2. Start a run with `tool_mode="auto"`.
3. Include the repo-local tools in `tool_allowlist`.
4. Let the in-engine agent decide when to call `bash`, `read`, `edit`, and `apply_patch`.
5. Stream events until the run completes.

Minimal example:

```python
import asyncio
from tandem_client import TandemClient


async def main():
    async with TandemClient(
        base_url="http://127.0.0.1:39731",
        token="tk_your_token",
    ) as client:
        session_id = await client.sessions.create(
            title="Autonomous repo coding agent",
            directory="/srv/repos/my-project",
            provider="custom",
            model="MiniMax-M2.7",
        )

        run = await client.sessions.prompt_async(
            session_id,
            (
                "Inspect the repository, run git status, review the current diff, "
                "and make the smallest safe change needed for the assigned task."
            ),
            tool_mode="auto",
            tool_allowlist=[
                "read",
                "glob",
                "grep",
                "bash",
                "write",
                "edit",
                "apply_patch",
            ],
        )

        async for event in client.stream(session_id, run.run_id):
            print(event.type, event.properties)
            if event.type in {
                "run.complete",
                "run.completed",
                "run.failed",
                "session.run.finished",
            }:
                break


asyncio.run(main())
```

### 2. Coder path for structured autonomous coding

Use this when you want Tandem's coder workflow to own execution.

This is the better fit when the work comes from issue intake, GitHub Projects, or other structured backlog flows.

Flow:

1. Create a coder run with the repo binding.
2. Call `execute_next()` or `execute_all()`.
3. Let Tandem create the internal worker session.
4. Let Tandem decide whether to use the repo root or a managed git worktree.
5. Let the worker use the normal built-in tool surface inside that workspace.

Minimal example:

```python
import asyncio
from tandem_client import TandemClient


async def main():
    async with TandemClient(
        base_url="http://127.0.0.1:39731",
        token="tk_your_token",
    ) as client:
        await client.coder.create_run(
            {
                "coder_run_id": "issue-123",
                "workflow_mode": "issue_fix",
                "model_provider": "custom",
                "model_id": "MiniMax-M2.7",
                "repo_binding": {
                    "project_id": "proj-1",
                    "workspace_id": "ws-1",
                    "workspace_root": "/srv/repos/my-project",
                    "repo_slug": "acme/my-project",
                },
                "github_ref": {
                    "kind": "issue",
                    "number": 123,
                },
            }
        )

        step = await client.coder.execute_next("issue-123")
        print(step)


asyncio.run(main())
```

## What coder does for you

For `issue_fix` workers, Tandem server creates a real session for the worker, points it at the worker workspace, and then gives that session an allowlist of `["*"]` before running it.

That means the coding worker can use the normal local tool surface without the external app wiring each tool call itself.

If the bound workspace is inside a git repository, coder also tries to create a managed worktree automatically. That is the right place for branch-isolated autonomous coding.

So for coder flows:

- the external app should not run `git checkout -b ...` itself
- the external app should not hand-maintain temporary worktrees
- the external app should let coder create and clean up the worker workspace

## Runtime inspection helpers

The Python SDK gives the external app a few useful inspection calls:

- `await client.list_tool_ids()`
- `await client.list_tools()`
- `await client.sessions.diff(session_id)`
- `await client.sessions.revert(session_id)`
- `await client.sessions.unrevert(session_id)`

Use these for visibility and recovery, not as a replacement for the model-driven tool loop.

## Approval and policy expectations

Allowing `bash` in the tool allowlist is necessary, but it is not the whole story.

The engine permission layer may still require approval for `bash`, depending on the active policy.

So for unattended autonomous coding, make sure:

- the relevant repo tools are in the allowlist
- the engine permission rules allow or auto-approve the needed command/file actions
- the workspace boundary is set correctly

If the agent can read files but keeps stalling on shell calls, the missing piece is usually permission policy rather than SDK wiring.

## GitHub MCP vs local git

Keep this split clear:

- use local repo tools for branches, worktrees, diffs, commits, edits, tests
- use GitHub MCP for remote GitHub operations such as project reads, issue comments, PR creation, review metadata, and project updates

Do not replace local git with a separate GitHub API adapter for repository changes.

## Best practice for the external agent

Tell the external agent to behave like this:

1. Create a session or coder run in Tandem.
2. Bind it to the target repo or project.
3. Allow the standard coding tools, especially `bash`.
4. Give the model the task.
5. Let Tandem own tool execution and workspace state.
6. Read progress and artifacts back through Tandem APIs.

Do not tell the external agent to:

- call `git` itself outside Tandem
- implement its own worktree lifecycle
- bypass Tandem's session/coder run model

## Short handoff text

Use this text when another agent keeps trying to hand-roll git:

> Use Tandem as the execution authority. Do not shell out to git from the outer Python app. Create a repo-bound session or a coder run through the Python SDK, allow the normal coding tools including `bash`, and let the in-engine worker call git through Tandem's tool loop. For structured coding flows, prefer coder so Tandem can create a managed worktree automatically. Use GitHub MCP only for remote GitHub actions, not for local repo mutation.
