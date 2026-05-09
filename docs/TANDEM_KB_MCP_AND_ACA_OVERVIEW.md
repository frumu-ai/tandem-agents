# Tandem KB MCP and ACA Autonomous Coder Technical Note

This note describes the two Tandem-adjacent systems in this repository at a technical level:

- the Tandem KB MCP, a retrieval-oriented MCP service for document discovery and fetch
- the ACA autonomous coder, a governed execution layer for real repository changes

The systems are complementary:

- KB MCP provides context
- ACA provides execution

## 1. System Boundaries

### Tandem KB MCP

The KB MCP is a read-oriented knowledge service. Its responsibility is to expose a stable agent-facing interface for:

- discovery of available collections
- collection-level guidance
- search across one or more collections
- fetch of a specific document by canonical identifier

It is intentionally retrieval-only. It should not become a second answer generator or a task runner.

### ACA autonomous coder

ACA is an orchestration layer that selects work, binds a repository, and runs a coded delivery loop. Its responsibility is to:

- accept work from a board or project source
- choose the execution target and workspace
- coordinate the manager, worker, reviewer, and tester phases
- persist run state, artifacts, and handoff data

ACA is not the execution authority. Tandem owns durable run state, workspace binding, and backend-controlled execution behavior.

## 2. Tandem KB MCP Contract

The KB MCP is shaped around a search-then-fetch workflow.

### Canonical flow

1. call `get_kb_guide()` when the agent does not yet know the server
2. inspect collection-level guidance with `get_collection_guide(collection_id)`
3. run `search_docs` to identify candidate documents
4. fetch the exact document by `doc_id`

### Contract details

- `search_docs` is the primary discovery primitive
- `doc_id` is the canonical fetch identifier
- `collection_id` is part of the result payload so cross-collection search remains usable
- `get_document` accepts the exact `doc_id` returned by search and listing calls
- `search_docs` supports optional `collection_id` scoping
- `search_docs` can also search all collections when the caller does not know the namespace

### Agent guidance surface

The KB MCP includes guide endpoints so the server can teach an agent how to use itself:

- `get_kb_guide()` explains when to use the KB, how to search, and which collections exist
- `get_collection_guide(collection_id)` summarizes a specific collection and identifies canonical documents

This is important because agents often need to bootstrap from arbitrary uploaded docs rather than from pre-configured knowledge of the namespace.

## 3. KB MCP Implementation Features

The KB MCP has a few Tandem-specific behaviors that make it more agent-friendly than a simple document index.

### Search behavior

- natural-language queries are normalized before FTS execution
- punctuation-heavy prompts are sanitized into FTS-safe terms
- fallback substring search is used only when the FTS path cannot parse the query
- result ranking prefers exact title and heading matches over body-only matches
- guide-like documents can receive a modest ranking boost for broad questions
- lower BM25 scores rank ahead of higher scores
- `kb_priority` can break near-ties

### Result payload

Search results include enough structure for an agent to decide whether to fetch a doc immediately:

- `doc_id`
- `collection_id`
- `title`
- `heading`
- `snippet`
- `score`
- `source_path`
- `kb_role` when present

### Guide generation

Collection guides are generated deterministically from indexed content.

- docs with `kb_role: guide` or `kb_summary` are preferred
- otherwise the guide is derived from titles, headings, tags, and excerpts
- canonical docs are selected by `kb_priority`, then recency, then title
- key topics are inferred from headings and tags rather than from naive word frequency

### Ingestion model

The KB accepts more realistic document layouts than a flat collection root.

- nested document trees are supported
- the system keeps a stable relative source path
- frontmatter metadata is merged instead of being overwritten by a second synthetic block
- `title` and `tags` remain part of the metadata model even when the document already has frontmatter

## 4. ACA Execution Model

ACA is a controlled workflow engine for coding tasks. The execution model is explicit and phase-based.

### Task intake

ACA can accept work from different sources:

- a local Kanban board
- a GitHub Project
- a direct operator-selected task

The task source is kept separate from the repository binding so task intake and code execution do not get conflated.

### Repository binding

Before code changes begin, ACA resolves:

- the target repository
- the writable workspace root
- the allowed paths for the run
- the active worktree or checkout

This explicit binding is critical because ACA should not assume the current process directory is the correct edit target.

### Runtime phases

The coding loop is intentionally bounded:

- manager phase for planning and decomposition
- worker phase for implementation
- reviewer phase for quality checks
- tester phase for validation

Tandem materializes the state for those phases so the run can be inspected and resumed more reliably than a prompt-only flow.

### State and artifacts

ACA writes durable outputs for the run:

- status snapshots
- blackboard state
- logs and progress events
- summaries and handoff notes
- diffs and artifacts

The run-state record is the source of truth for progress, not the transient chat conversation.

## 5. Tandem-Backed Execution Features

The Tandem runtime gives ACA the operational primitives needed for real delivery work.

- managed worktrees isolate individual runs
- durable coordination state tracks claims, leases, workers, and events
- explicit approvals and transitions reduce ambiguity
- backend reuse avoids reinitializing state unnecessarily
- worker cleanup is part of the lifecycle
- bounded multi-agent fan-out is supported when tasks need parallel effort
- browser-assisted QA can be attached when a task needs end-to-end verification

These are the features that turn ACA from a prompt-driven coding demo into a controlled system for actual repository changes.

## 6. End-to-End Data Flow

A typical run moves through the system like this:

1. ACA selects a task source and task record
2. ACA resolves repository binding and workspace configuration
3. ACA asks Tandem to claim the task and create or reuse a managed worktree
4. Tandem records the run state and current coordination metadata
5. ACA uses the KB MCP when it needs docs, policies, or setup guidance
6. ACA runs manager, worker, reviewer, and tester phases
7. Tandem persists events, artifacts, and final state transitions
8. ACA syncs the outcome back to the source system
9. Tandem releases the workspace and any worker resources

## 7. Operational Guarantees

The design is built around a few invariants:

- the KB MCP should be deterministic and read-only in behavior
- ACA should never guess the repository scope
- durable state should outlive a single prompt or process
- task claims and transitions should be explicit
- partial progress should be preserved when possible
- validation should happen before a run is marked complete

## 8. Failure and Recovery Model

The system is expected to tolerate imperfect runs.

- if a worker crashes, the worktree state can be preserved for inspection
- if remote sync fails, the latest good snapshot remains visible
- if capacity is full, new claims should backpressure rather than overcommit
- if cancellation is requested, new claims should stop and artifacts should remain available

## 9. Summary

At a technical level, the split is:

- KB MCP provides a stable, searchable knowledge interface for agents
- ACA provides a governed, stateful execution pipeline for code work

The two together give Tandem an agent workflow that is both context-aware and operationally safe.
