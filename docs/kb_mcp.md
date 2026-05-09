# KB MCP Agent-Guide Upgrade Plan

## Summary

Improve the KB MCP so agents can reliably discover it, understand when to use it, and get a usable guide from arbitrary uploaded docs without requiring a separate manual setup step.

The implementation should focus on four outcomes:

- fix the current MCP contract mismatch so agents can round-trip search results into document fetches
- generate a collection-level guide from uploaded content, with optional stronger guidance from user-authored docs/frontmatter
- make search work for natural-language agent questions and support cross-collection discovery when the agent does not know the namespace yet
- expand ingestion so the KB can handle more realistic doc layouts and metadata without silently dropping content

## Key Changes

### MCP contract and agent guidance

- Change `get_document` so it accepts the exact `doc_id` returned by `search_docs` and `list_documents`.
- Keep `collection_id + slug` support as a backwards-compatible alias, but make `doc_id` the canonical identifier in tool descriptions and responses.
- Add `get_collection_guide(collection_id)` as a first-class read-only tool.
- Keep `get_kb_guide()` as the global entrypoint, but make it return:
  - when to use the KB
  - how to search it
  - available collections
  - recommended agent flow
- Update MCP `initialize.instructions` and `tools/list` descriptions so an agent is explicitly told:
  - use this MCP for business docs, policies, onboarding, support, product, pricing, and FAQ-style content
  - call `get_kb_guide` first when unfamiliar with the server
  - call `search_docs` first for question answering and `get_document` only when full context is needed

### Guide generation from uploaded docs

- Generate a guide for every collection automatically from indexed content.
- Add optional guide-aware metadata in markdown frontmatter:
  - `kb_role`: one of `guide`, `faq`, `policy`, `reference`, `runbook`
  - `kb_summary`: short human-authored collection/doc summary
  - `kb_keywords`: preferred search terms
  - `kb_priority`: integer ranking hint for canonical docs
- `get_collection_guide(collection_id)` should return:
  - `collection_id`
  - `summary`
  - `recommended_use_cases`
  - `recommended_queries`
  - `canonical_documents`
  - `key_topics`
  - `doc_count`
  - `last_updated_at`
- Guide generation rules:
  - prefer docs marked `kb_role: guide` or with `kb_summary`
  - otherwise derive guide content from titles, headings, tags, and excerpted content across the collection
  - choose canonical documents by `kb_priority` first, then recency, then title
  - derive key topics from headings and tags, not from arbitrary raw-word frequency
- `get_kb_guide()` should include a short global guide plus embedded collection summaries for the top collections.

### Ingestion and metadata correctness

- Fix admin create/update so explicit `title` and `tags` always merge into frontmatter rather than being skipped when frontmatter already exists.
- Preserve and update existing frontmatter keys instead of prepending a second synthetic block.
- Extend ingestion to support nested paths under a collection, not only `/collection/file.md`.
- Store a stable relative source path so nested docs can be addressed and debugged clearly.
- Keep `.md` and `.txt` support in v1, but make nested trees valid for both.
- Reserve special behavior for guide docs only through frontmatter, not filename magic.

### Search and retrieval quality

- Change `search_docs` so `collection_id` is optional.
- If `collection_id` is omitted, search across all collections and return `collection_id` with every hit.
- Normalize natural-language queries before FTS:
  - tokenize and escape unsupported punctuation
  - build an FTS-safe query from meaningful terms
  - fall back to substring search only when FTS parsing still fails
- Rank results with this precedence:
  - `kb_role: guide` and `kb_role: faq` get a modest boost for broad questions
  - exact title/heading matches rank above body-only matches
  - lower BM25 score ranks above higher score
  - `kb_priority` breaks near-ties
- Return richer search results:
  - `doc_id`
  - `collection_id`
  - `title`
  - `heading`
  - `snippet`
  - `score`
  - `source_path`
  - `kb_role` if present
- Keep `list_collections` and `list_documents`, but ensure they expose enough summary metadata for agents to choose a collection without opening full docs.

## Test Plan

- MCP contract tests:
  - `search_docs` result `doc_id` can be passed directly to `get_document`
  - `get_kb_guide` and `get_collection_guide` return deterministic guide payloads
  - `search_docs` works with and without `collection_id`
- Metadata tests:
  - create/update correctly merges explicit `title` and `tags` into existing frontmatter
  - `kb_role`, `kb_summary`, `kb_keywords`, and `kb_priority` are parsed and surfaced correctly
- Search tests:
  - natural-language queries with punctuation do not break FTS
  - exact title/heading matches outrank weak body matches
  - guide/faq docs are boosted but do not swamp clearly better exact matches
  - cross-collection search returns mixed collections with correct identifiers
- Ingestion tests:
  - nested docs are indexed and searchable
  - deleted nested docs are removed from the index
  - collection guide generation still works when no explicit guide doc exists
- Acceptance scenarios:
  - an agent can connect, call `get_kb_guide`, discover the KB purpose, and answer a question using `search_docs`
  - a user uploads ordinary docs with no special metadata and still gets a usable generated guide
  - a user uploads a `kb_role: guide` doc and the agent receives stronger, more directed collection guidance

## Assumptions and Defaults

- The KB remains retrieval-only; it does not generate final answers itself.
- Frontmatter metadata is optional, not required.
- `doc_id` becomes the canonical fetch identifier.
- Cross-collection search is enabled by default because agents often will not know the right collection ahead of time.
- Guide generation is deterministic and rules-based, not LLM-generated.

## Kanban Board

```yaml
board:
  id: kb-mcp-agent-guide
  name: KB MCP Agent Guide Upgrade
  columns:
    - backlog
    - ready
    - in_progress
    - review
    - test
    - blocked
    - done

cards:
  - id: kb-contract-guide-surface
    title: Fix KB MCP contract and add explicit agent guide tools
    lane: ready
    description: >
      Repair the MCP identifier contract so agents can round-trip search results
      into document fetches, then add a global and collection guide surface that
      explicitly teaches agents when and how to use the KB.
    acceptance_criteria:
      - get_document accepts the exact doc_id returned by search_docs and list_documents
      - get_kb_guide returns usage instructions plus collection summaries
      - get_collection_guide(collection_id) exists and is exposed in tools/list
      - initialize.instructions clearly tells agents to search the KB for doc questions
    labels: [kb, mcp, agent-ux, contract]
    priority: high
    source: { type: manual, ref: kb-review }
    repo: { path: . }
    subtasks:
      - Make doc_id the canonical fetch identifier
      - Add get_collection_guide tool and schema
      - Strengthen initialize.instructions and tool descriptions
    history: []

  - id: kb-guide-generation
    title: Generate collection guides from uploaded docs
    lane: ready
    description: >
      Add deterministic guide generation so every collection can explain its own
      scope and best-use docs, with optional stronger hints from frontmatter.
    acceptance_criteria:
      - each collection has a generated guide even with plain uploaded docs
      - kb_role, kb_summary, kb_keywords, and kb_priority are parsed from frontmatter
      - guide payload includes summary, use cases, canonical docs, topics, and freshness
      - guide docs and priority hints influence canonical document selection
    labels: [kb, indexing, guide, metadata]
    priority: high
    source: { type: manual, ref: kb-review }
    repo: { path: . }
    subtasks:
      - Add guide metadata parsing rules
      - Build collection guide derivation in the index layer
      - Expose generated guide through MCP responses
    history: []

  - id: kb-search-discovery
    title: Improve search for natural-language agent questions
    lane: backlog
    description: >
      Make search robust for conversational prompts and allow discovery across
      all collections when the agent does not know the namespace up front.
    acceptance_criteria:
      - search_docs works without collection_id and returns collection-aware results
      - FTS queries are normalized and escaped before execution
      - fallback search is only used when FTS cannot handle the query
      - ranking boosts exact heading/title matches and guide-like docs appropriately
    labels: [kb, search, retrieval, ranking]
    priority: high
    source: { type: manual, ref: kb-review }
    repo: { path: . }
    subtasks:
      - Make collection_id optional in search_docs
      - Add query normalization and FTS-safe tokenization
      - Improve ranking and result payload fields
    history: []

  - id: kb-ingestion-layout
    title: Expand ingestion coverage for realistic doc sets
    lane: backlog
    description: >
      Support nested document trees and correct metadata merging so the KB can
      index real exported docs, handbooks, and product docs without silent loss.
    acceptance_criteria:
      - nested docs under a collection are indexed and searchable
      - source paths remain stable and visible in results
      - create/update merges title and tags into existing frontmatter correctly
      - delete and reconcile flows work for nested docs
    labels: [kb, ingestion, storage, metadata]
    priority: medium
    source: { type: manual, ref: kb-review }
    repo: { path: . }
    subtasks:
      - Replace two-level path assumption with collection-rooted relative paths
      - Merge title and tags into frontmatter instead of prepending blindly
      - Cover nested delete/reindex behavior
    history: []

  - id: kb-test-matrix
    title: Add retrieval and guide generation regression coverage
    lane: backlog
    description: >
      Extend the KB test suite so contract, guide generation, metadata, search,
      and nested-ingestion behavior are protected against regressions.
    acceptance_criteria:
      - tests cover doc_id round-trip, global guide, collection guide, and cross-collection search
      - tests cover title/tag merge behavior with existing frontmatter
      - tests cover nested-doc indexing and deletion
      - tests cover natural-language queries with punctuation and quotes
    labels: [kb, tests, quality]
    priority: medium
    source: { type: manual, ref: kb-review }
    repo: { path: . }
    subtasks:
      - Add MCP contract regression tests
      - Add guide generation fixtures
      - Add ingestion and search edge-case tests
    history: []
```
