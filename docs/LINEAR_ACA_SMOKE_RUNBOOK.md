# Linear ACA Smoke Runbook

This runbook covers TAN-126: proving the Linear-to-PR-to-merge ACA coding loop
without mutating real Linear or GitHub resources by default.

## What The Mocked Smoke Covers

Run:

```bash
python3 -m unittest src.tandem_agents.core.verification.linear_aca_smoke_test
```

The smoke drives the ACA supervisor through these states with mocked MCP calls:

- Linear task reaches coder completion and queues Linear `In Review` sync.
- ACA creates/reuses PR metadata for an ACA branch.
- PR review feedback triggers a bounded repair pass.
- Ready-to-merge PR pauses when merge approval is required.
- Merge approval alone permits merge and leaves branch deletion pending.
- Merge plus branch-delete approval permits guarded cleanup.
- Successful merge queues Linear `Done` sync.

No live Linear or GitHub issue, PR, branch, or comment is changed by this test.

## Human Approval Gates

Finalization has separate policy controls:

```yaml
review:
  policy: auto_merge
  auto_merge_strategy: squash
  auto_merge_allowed_strategies: squash
  merge_requires_approval: true
  branch_delete_requires_approval: true
  delete_branch_after_merge: true
```

The default is conservative:

- `merge_requires_approval: true` makes ACA stop at `pending_approval` before
  merging.
- `branch_delete_requires_approval: true` makes branch cleanup a separate
  approval after merge.
- `delete_branch_after_merge: false` disables remote branch cleanup even when
  merge is allowed.

For an unattended smoke or production dry-run, set the relevant approval gates
to `false` explicitly. Do not rely on `review.policy: auto_merge` alone as a
blanket permission.

## Local Readiness Before Live Testing

Before using real Linear/GitHub resources, validate the local stack:

```bash
./scripts/run.sh --validate
./scripts/run.sh --check-engine
./scripts/run.sh --print-config
```

The live loop needs:

- Tandem engine reachable at the configured `tandem.base_url`.
- `task_source.type: linear` with the target Linear team/project/item.
- `linear_mcp.enabled: true` and scope `intake_finalize` or `always`.
- GitHub MCP configured for PR creation/finalize operations.
- A repository binding for the repo ACA will edit.
- Provider/model settings that can run the selected backend.

## Cross-Repo Contract

`tandem-agents` owns ACA orchestration:

- Linear intake and status/comment outbox records.
- GitHub PR lifecycle supervision.
- Review-note repair dispatch.
- Merge/delete approval policy and local run artifacts.

`tandem` owns execution and MCP capability readiness:

- Tandem engine health and coder runs.
- Connected Linear/GitHub MCP servers and tool capability readiness.
- Control-panel cockpit surfaces for selected ACA task/run state.
- Feedback delivery into ACA coordination.

The smoke assumes Tandem exposes Linear and GitHub MCP tools through the
configured engine, but the default mocked test replaces those calls so it can
run in CI or on a laptop without OAuth side effects.

## Inspecting Artifacts

For a real run, inspect:

```bash
./scripts/monitor.sh --run-dir runs/<run_id>
./scripts/run.sh operator-status
./scripts/run.sh coordination-status
```

Important files:

- `runs/<run_id>/status.json`
- `runs/<run_id>/blackboard.yaml`
- `runs/<run_id>/events.jsonl`
- `runs/<run_id>/summary.md`

For approval debugging, check:

- `blackboard.yaml` field `finalization_approvals`
- `status.json` field `pull_request_merge.pending_approvals`
- event `github_pull_request.auto_merge_evaluated`
