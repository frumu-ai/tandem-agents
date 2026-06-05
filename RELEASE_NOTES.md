# Release Notes

## vNext (Unreleased)

### Linear Issue Intake

- ACA can now use `task_source.type=linear` to pull work from Linear teams and
  projects through Tandem's connected Linear MCP server.
- Linear intake supports team/project filters, launch-status filters, label
  filters, optional search queries, explicit issue selectors, and a unified
  scheduler board snapshot.
- ACA records Linear issue metadata on normalized tasks while keeping repo
  binding separate from Linear project context.
- Claim and finalize sync can update Linear issue status, apply ACA labels, and
  add a run summary comment through the coordination outbox.
- Linear OAuth remains owned by the Tandem control panel MCP connection; ACA
  stores only the MCP server name and sync policy.
