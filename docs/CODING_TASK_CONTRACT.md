# ACA Coding Task Contract

Last updated: 2026-06-24
Status: Canonical ACA contract

## Purpose

This document is the public contract for coding tasks that ACA accepts from a
task source and hands to a coder runtime.

It defines the minimum task envelope, the information every coder tool must
receive, and the debugger fields operators need to understand a run. Private
deployment details, credentials, and hosted backend topology are outside this
contract.

## Task Envelope

ACA normalizes every task source into one task envelope before scheduling or
execution. The envelope should preserve these fields when the source provides
them:

| Field | Required | Meaning |
| --- | --- | --- |
| `task_key` or `task_id` | Yes | Stable task identifier from the board, Linear issue, or another source. |
| `source` | Yes | Source type and source-specific reference or URL. |
| `title` | Yes | Human-readable task title. |
| `program_goal` | No | Larger initiative this task contributes to. |
| `local_goal` | No | Specific result expected from this task. Defaults to the title when absent. |
| `repo` | Yes for code edits | Repository slug, path, default branch, and remote metadata used for checkout. |
| `in_scope` | No | Work that is explicitly allowed. |
| `out_of_scope` | No | Work that must not be changed in this task. |
| `target_files` | No | Files or directories the task expects to inspect or edit. |
| `dependencies` | No | Task references that must be done before this task is admitted. |
| `deliverables` | No | Expected artifacts, behavior, docs, or PR output. |
| `acceptance_criteria` | No | Conditions that make the task done. |
| `verification_commands` | No | Commands or checks expected to prove the change. |
| `notes_for_agent` | No | Extra safe implementation context for the coder. |
| `subtasks` | No | Optional smaller work items for fan-out or specialized agents. |

For code edits, `repo` must be explicit before a run starts. If the source does
not provide enough repository binding to choose a checkout safely, ACA must
block the task instead of guessing.

## Source Mapping

Task sources may provide the envelope directly or through Markdown sections.
ACA recognizes common headings such as `Program context`, `Local goal`, `Scope`,
`Dependencies`, `Deliverables`, `Target files`, `Verification`, `Acceptance`,
`Notes for agent`, and `Subtasks`.

When present, a `tandem:coder_handoff:v1` HTML comment can add structured
handoff data from an upstream triage system. That handoff may contribute likely
files, verification steps, acceptance criteria, failure context, and risk notes.
The JSON object inside the comment must include
`"handoff_type": "tandem_autonomous_coder_issue"`; ACA ignores handoff blocks
without that type so unrelated comments cannot be interpreted as coding tasks.

Example:

```markdown
<!-- tandem:coder_handoff:v1
{
  "handoff_type": "tandem_autonomous_coder_issue",
  "likely_files": ["src/example.py"],
  "verification_commands": ["python3 -m unittest tests.example_test"]
}
-->
```

The current implementation lives in:

- `src/tandem_agents/core/task_contract.py`
- `src/tandem_agents/runtime/task_sources.py`
- `src/tandem_agents/core/coordination/tasks.py`

## Coder Tool Contract

Every coder prompt or tool handoff for a repository edit must include enough
context to make a bounded, reviewable change:

- workspace root or worktree path
- repository slug or repository path
- branch name when one has been allocated
- allowed paths, denied paths, or target files when known
- task goal and acceptance criteria
- expected deliverables
- verification commands or the narrowest expected check
- dependency status and any active blockers

Coder output must preserve enough information for review and handoff:

- changed files and a concise diff summary
- verification commands run
- verification result, including failure output when a check fails
- blockers or unresolved risks
- PR URL when a PR was opened
- summary of any work intentionally left undone

If a coder cannot verify the change, it must report the exact command that was
skipped or failed and why. A task is not complete solely because a diff exists.

## Runtime And Debugger Surfaces

The same fields must remain visible after intake so operators can debug stalled
or failed runs.

| Surface | Contract status | Evidence |
| --- | --- | --- |
| Task normalization | Covered | `src/tandem_agents/core/task_contract.py` parses and normalizes the source task contract. |
| Repository binding and run isolation | Covered | `src/tandem_agents/core/phases/task_intake.py` and `src/tandem_agents/core/repository/repository.py` allocate task branches and worktrees. |
| Coder prompt contract | Covered | `src/tandem_agents/core/engine/prompts.py` includes goal, scope, target files, deliverables, verification, and acceptance criteria in worker prompts. |
| Run state | Covered | `src/tandem_agents/runtime/runstate.py` writes `status.json`, events, artifacts, and the normalized task payload. |
| Operator summary | Covered | `src/tandem_agents/runtime/operator_view.py` exposes ownership, lease, worker, branch, PR, blocker, verification, target file, and task contract fields. |
| Operator dashboard | Covered | `src/tandem_agents/runtime/operator_dashboard.py` renders task status, ownership, branch, PR, backend, path, and blocked reason. |
| Monitoring docs | Covered | `docs/MONITORING.md` and `docs/RUN_STATE_SCHEMA.md` define the run files operators inspect. |
| Desktop, TUI, and Studio clients | External consumer | This repository does not contain those client surfaces. They should consume this contract or mark unsupported fields explicitly. |

Any new ACA surface that displays task or run state must either preserve the
fields above or document why the field is not available in that context.

## Completion Contract

A coding run is eligible for PR handoff only when the run summary can answer:

- which task source and task id were used
- which repo binding, branch, and worktree were used
- which files changed
- which verification command ran and whether it passed
- which failure output or blocker remains, if any
- which PR was opened or why no PR was opened
- which owner should act next

Review and merge automation must treat missing verification, missing changed
files, and missing PR metadata as blocked states unless the task is explicitly a
comment-only or audit-only task.

## Memory Policy

This contract does not require a new memory promotion or retrieval policy. The
current repo gap for TAN-59 is the public task/tool contract itself. If a future
memory surface stores task summaries, it should store only safe handoff facts:
task id, repo, changed files, verification result, PR link, and residual risk.
