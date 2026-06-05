# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to semantic versioning (`MAJOR.MINOR.PATCH`).

## [Unreleased]

### Added
- Initial public release.
- Third-party notices and package license metadata for public repository readiness.
- Local quickstart guide for first-time Docker Compose evaluation.
- First-class Linear issue task source support for ACA, resolved through the
  Tandem engine MCP registry instead of local Linear tokens.
- Linear intake preview and board snapshots with team/project filters,
  launch-status filtering, scheduler-approved issue selection, and normalized
  ACA task contracts.
- Linear remote sync for claim/finalize status, labels, and run summary
  comments through the coordination outbox.

### Fixed
- Board loading syntax error in the legacy board-shape fallback.
- `ACA_GITHUB_MCP_ENABLED` environment override handling.
- Duplicate pull request avoidance for existing GitHub MCP PRs with a matching head branch.
