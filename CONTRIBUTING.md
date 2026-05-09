# Contributing to Tandem Agents

Thanks for your interest in contributing. This document covers what we accept,
how to set up a working environment, the workflow for submitting changes, and
the licensing implications of contributing.

## What we accept

- Bug reports with a minimal reproduction.
- Bug fixes for correctness or robustness issues, ideally with a regression
  test.
- New tests that lock in existing behavior or cover an edge case we missed.
- Documentation fixes and clarifications.
- Targeted features that align with the project's scope (autonomous coding
  agent orchestration, knowledgebase-backed agent flows, control-plane
  ergonomics).

For larger features, **open an issue first** so we can discuss scope before
you spend time on implementation. We may decline contributions that expand
scope beyond the current direction or that overlap with planned commercial
features.

## What we don't accept

- Feature flags or shims that exist only to support a private fork.
- Changes that bypass coordination, lease, or approval safety rails.
- Vendor-specific integrations behind closed APIs.
- Changes whose primary purpose is to remove or weaken license restrictions.

## Development environment

The project runs the same way on a laptop, in Docker Compose, or on a hosted
Linux box. The path of least resistance for contributors is Docker Compose.

```bash
cp .env.example .env
./scripts/setup.sh
./scripts/build-containers.sh
docker compose up -d
```

See [docs/DOCKER_COMPOSE.md](docs/DOCKER_COMPOSE.md) for the full operator
walkthrough and [AGENTS.md](AGENTS.md) for the contributor read order.

## Running tests

Backend unit tests run inside the `aca` container:

```bash
docker compose exec aca python3 -m unittest discover -s src/tandem_agents -p "*_test.py"
```

Targeted runs are faster:

```bash
docker compose exec aca python3 -m unittest \
  src.tandem_agents.core.coordination.coordination_test \
  src.tandem_agents.config.config_loader_test
```

For UI / control-panel work, run `npx tsc --noEmit` against the control-panel
package and verify behavior in the browser.

## Submitting changes

1. **Open an issue first** for non-trivial changes. Describe the problem
   you're solving, the proposed approach, and any alternatives considered.
2. Fork the repo and create a topic branch from `main`.
3. Keep commits focused. Prefer many small, well-described commits over one
   sprawling commit.
4. Run the test suite before pushing.
5. Open a pull request against `main`. The PR description should:
   - Explain the *why*, not just the *what*.
   - List any behavior changes, breaking changes, or migration steps.
   - Reference the issue it addresses.
6. Be patient with review. We try to respond within a week.

## Style

- Python: follow the existing module style. No new linters introduced
  unless the user explicitly asks. Keep functions short and named clearly.
- Don't add comments that just restate the code. Comments should explain
  the *why* — non-obvious constraints, hidden invariants, or workarounds.
- No new dependencies without a clear reason; prefer the standard library.

## Security issues

Do **not** open a public issue for a security vulnerability. See
[SECURITY.md](SECURITY.md) for the disclosure process.

## License and contributor agreement

This project is licensed under the **Business Source License 1.1** (see
[LICENSE](LICENSE)). By submitting a contribution you agree that:

- You have the right to submit the contribution under this project's license.
- Your contribution is licensed under the same Business Source License 1.1
  as the rest of the project.
- The project Licensor (Frumu LTD) may use, modify, sublicense, and
  redistribute your contribution under the same license, including under
  the Change License (GPL-2.0-or-later) once the Change Date is reached
  for the relevant version.
- You retain copyright in your contribution; this is a license, not an
  assignment.

If you cannot agree to the above, do not open a pull request. For commercial
licensing or larger collaboration arrangements, contact info@frumu.ai.
