# Docker Compose For ACA

This directory can be run inside Docker Compose to keep the agent environment reproducible.

## What The Container Does

- `tandem-engine` runs as a health-checked sidecar
- `tandem-control-panel` runs as a health-checked companion UI
- `tandem-kb-mcp` runs as a local knowledgebase MCP service for docs upload and search
- the sidecar builds from `config/Dockerfile.engine`
- the control panel builds from `config/Dockerfile.control-panel`
- the KB service builds from `config/Dockerfile.kb`
- the engine keeps persistent state under `./tandem-engine-state`
- the control panel keeps persistent state in a named Docker volume
- shared Tandem output lives under `./tandem-data`
- KB docs and the SQLite index live under `./kb-data`
- the Tandem token lives in `./tandem-data/tandem_api_token`; the engine sidecar can create it on first start, and the runner mounts it read-only
- the sidecar stays internal to Compose; ACA and the control panel talk to it over the Compose network
- the control panel is published to the host on `39734` by default
- the KB service is published to the host on `39736` by default
- the `aca` runner container loads `./.env` when present
- repo checkouts are mounted to the host under `./workspace/repos` by default
- local host repos can be bind-mounted through `ACA_LOCAL_REPOS_DIR` to `/workspace/test-repos`
- the runner checks Tandem engine reachability and reuses the sidecar when possible
- the runner resolves a repository binding
- the runner clones the repository into its workspace if needed
- the runner creates isolated worktrees for worker tasks

## Suggested Workflow

1. Copy `.env.example` to `.env`.
2. Keep `TANDEM_API_TOKEN_FILE` pointed at your token file path unless you need a custom mount location.
3. Keep `TANDEM_API_TOKEN` blank unless you are using a legacy non-Docker fallback.
4. Leave repository binding, task source, provider, storage profile, and swarm choices to the control panel install config unless you need to override them manually.
5. Leave `TANDEM_BASE_URL` blank for the Compose sidecar; ACA will use the service URL automatically.
6. Keep `TANDEM_PORT` at `39733` unless you have a reason to change the internal sidecar port.
7. Keep `TANDEM_CONTROL_PANEL_PORT` at `39734` unless that host port is already in use.
8. If `39733` or `39734` is already in use, change the matching env var before starting Compose.
9. If you want a standalone Tandem engine, point `TANDEM_BASE_URL` at it in a non-Compose run or an override file.
10. For the fastest first run, use the published GHCR images: `docker compose -f docker-compose.published.yml pull` and then `docker compose -f docker-compose.published.yml up -d`. Set `TANDEM_AGENTS_IMAGE_TAG=v0.5.6` to pin a specific published release.
11. To build the images locally from this checkout instead, run `./scripts/build-containers.sh`. By default this pulls the latest Tandem engine and control panel releases; set `TANDEM_ENGINE_RELEASE_VERSION` or `TANDEM_CONTROL_PANEL_RELEASE_VERSION` first if you want to pin one package. `TANDEM_RELEASE_VERSION` still pins both for backwards compatibility.
12. On the first boot, the Tandem engine container will create `tandem-data/tandem_api_token` automatically if it is missing.
13. Open `http://127.0.0.1:39734` on the server host, or `http://<server-ip>:39734` from another device.
14. Sign in to the control panel with the token from `tandem-data/tandem_api_token`.
15. Use `./workspace/repos`, `./runs`, `./tandem-data`, `./secrets`, and the container logs to inspect repo checkouts, artifacts, and engine state from the host.
16. For local-repo testing, place repos under `./test-repos` or set `ACA_LOCAL_REPOS_DIR` to another host directory.
17. Keep `ACA_STORAGE_PROFILE=local` for the single-machine SQLite-backed flow, or set it to `shared` when you want the Postgres coordination backend for shared deployments.
18. For shared mode, either point `ACA_COORDINATION_POSTGRES_URL` at an external Postgres instance or let Compose use the `coordination-postgres` service by starting with `docker compose --profile shared up`.
19. The Compose defaults assume a local Postgres database named `aca` with user/password `aca`; override `ACA_COORDINATION_POSTGRES_DB`, `ACA_COORDINATION_POSTGRES_USER`, and `ACA_COORDINATION_POSTGRES_PASSWORD` if you want different credentials.
20. Keep `KB_DOCS_ROOT` and `KB_INDEX_ROOT` pointed at `./kb-data` unless you need a custom mount location for the knowledgebase service.

If you start with `docker-compose.published.yml`, use the same `-f` flag for
follow-up `docker compose exec`, `logs`, and `down` commands.

## Binding Behavior

- If `TANDEM_BASE_URL` is set and reachable, the runner reuses that Tandem engine instead of starting a duplicate.
- If the Compose sidecar is enabled, ACA waits for the sidecar healthcheck and then reuses it with `TANDEM_ENGINE_STARTUP_MODE=reuse_only`.
- If the Compose control panel is enabled, it points at the same `tandem-engine` service ACA uses.
- If `ACA_REPO_PATH` is set and exists inside the runner container, the runner uses that checkout.
- If you use the default local repo mount, set `ACA_REPO_PATH` to `/workspace/test-repos/<repo-name>`.
- If you want to use a host checkout, bind mount it into the runner container at the same path before starting Compose.
- If `ACA_REPO_SLUG` is set, the runner clones it into the mounted `/workspace/repos` workspace.
- If `ACA_REPO_URL` is set, the runner clones it directly.
- By default, `/workspace/repos` is backed by host `./workspace/repos` so you can inspect live repo checkouts outside Docker.
- `ACA_WORKTREE_ROOT` controls the clone/worktree workspace inside the container.
- `ACA_OUTPUT_ROOT` controls the writable run-artifact directory inside the container.
- `ACA_COORDINATION_POSTGRES_URL` controls the shared coordination backend when `ACA_STORAGE_PROFILE=shared`.

## Autonomous Permissions

- ACA session-backed runs auto-approve their own pending Tandem permission requests for the active session.
- This is required because Tandem's default permission templates still mark `bash` as `ask`.
- In this ACA stack, unattended coding runs should not stall on approval prompts for repo-local shell commands.

## Auth Notes

GitHub Projects should use a GitHub PAT so Tandem can bootstrap the official GitHub MCP server non-interactively in containers:

- prefer `GITHUB_PERSONAL_ACCESS_TOKEN`
- use `GITHUB_TOKEN` as a fallback when needed
- prefer the mounted secret file form when possible: `GITHUB_PERSONAL_ACCESS_TOKEN_FILE`
- use `GITHUB_TOKEN_FILE` as the fallback file path when needed
- avoid relying on interactive login for automated runs
- do not require a separate GitHub CLI login for the default GitHub Projects path

Provider auth should usually use `ACA_PROVIDER_KEY` in `.env` for simple single-provider setups.
ACA will map it onto the active provider's expected env var for Tandem subprocesses.

Provider-specific secret env vars are still available when you want them explicitly:

- `OPENROUTER_API_KEY` for `ACA_PROVIDER=openrouter`
- `OPENAI_API_KEY` for `ACA_PROVIDER=openai`, including OpenAI-compatible custom base URLs
- the other `*_API_KEY` vars for their matching providers

The provider-specific env vars are clearer for shared or multi-provider setups.

The Tandem API token should live in a mounted secret file instead of `.env`.
ACA reads `TANDEM_API_TOKEN_FILE` from the container environment, and the Tandem wrapper loads the file and exports `TANDEM_API_TOKEN` only inside the engine process.
If the secret file is missing, the engine wrapper generates one automatically on first start.
If you want to inspect the sidecar from the host, use `docker compose exec tandem-engine tandem-engine status`.
If you want to seed the file manually, you can still run `tandem-engine token generate > tandem-data/tandem_api_token`.
The ACA Compose control panel expects that same token at login, so engine auth and UI auth stay aligned by default.

## Next Integration Step

When the actual portable runner exists, wire it into `scripts/entrypoint.sh` after repo resolution and engine status checks.
