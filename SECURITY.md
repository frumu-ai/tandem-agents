# Security Policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in Tandem Agents,
please report it privately. **Do not open a public GitHub issue.**

Email: **security@frumu.ai**

Please include:

- A clear description of the issue.
- The version (or commit hash) where you observed it.
- Reproduction steps. A minimal proof-of-concept is ideal.
- Any potential impact you have assessed (data exposure, RCE, privilege
  escalation, denial of service, etc.).
- Whether you would like to be credited in the resulting advisory.

We will acknowledge your report within **3 business days** and will keep
you informed as we investigate. We aim to provide an initial assessment
(severity, expected timeline) within **10 business days**.

## Disclosure timeline

- We follow **coordinated disclosure**. Once a fix is available, we will
  publish a security advisory through GitHub Security Advisories and credit
  the reporter (unless you ask to remain anonymous).
- We aim to release fixes within **90 days** of a confirmed report. For
  critical issues, we may release out-of-band patches sooner.
- Please do not publicly disclose details before a fix has been released.

## Scope

In scope:

- The Tandem Agents control plane: orchestration loop, coordination ledger,
  outbox dispatcher, lease management, run state writers, CLI / API.
- The Knowledgebase MCP server bundled in this repository.
- Container definitions and compose stack as published.

Out of scope:

- The Tandem engine itself (separate project, separate report channel).
- The Tandem control panel UI (separate project).
- Issues in third-party dependencies — please report those upstream. If a
  dependency CVE materially affects Tandem Agents we will track it as a
  build-of-materials advisory and ship an updated pin.
- Findings that require operator action against their own deployment to
  exploit (e.g. running in development mode with `ACA_API_REQUIRE_TOKEN=false`,
  exposing the API to the public internet, using default secrets in
  production). The defaults in this repository are designed to fail closed;
  opting out is your responsibility.

## Hardening defaults

Tandem Agents ships with a default-secure posture. Notable invariants:

- `ACA_API_REQUIRE_TOKEN=true` by default — the API server refuses to start
  without a configured token.
- The pre-commit and pre-push hooks in `.githooks/` invoke gitleaks to
  prevent committing secrets. Enable with `bash scripts/setup-githooks.sh`
  after cloning.
- Secrets live under `secrets/` and `.env`, both gitignored.
- The coordination lease lifecycle is wrapped in try/except/finally so a
  worker crash always releases its lease rather than orphaning it.

If you find a way to bypass any of these defaults, that's in scope.

## Acknowledgments

We will list disclosed-and-fixed advisories in the project changelog and
credit reporters who wish to be named.
