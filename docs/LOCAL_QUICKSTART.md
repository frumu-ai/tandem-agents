# Local Quickstart

This guide gets Tandem Agents running on one machine with Docker Compose.
It is the recommended path for first-time local evaluation.

## Prerequisites

- Docker with Docker Compose v2
- Node.js and npm, used by `scripts/build-containers.sh` to resolve Tandem package versions
- Git
- A model provider API key, usually one of:
  - `OPENAI_API_KEY`
  - `OPENROUTER_API_KEY`
  - `ANTHROPIC_API_KEY`

GitHub Projects intake also needs a GitHub PAT, but it is optional for a first
local-board run.

## 1. Bootstrap Local Config

```bash
cp .env.example .env
./scripts/setup.sh
```

The setup script creates local token files under `tandem-data/` and writes the
control-panel config to `tandem-data/control-panel-config.json`.

## 2. Add A Provider Key

Edit `.env` and set the provider key you want to use. For example:

```bash
OPENAI_API_KEY=...
```

Then make sure the provider and model match your key. The defaults are:

```bash
ACA_PROVIDER=openai
ACA_MODEL=gpt-4.1-mini
```

For OpenRouter, use:

```bash
ACA_PROVIDER=openrouter
ACA_MODEL=<openrouter-model-id>
OPENROUTER_API_KEY=...
```

## 3. Inspect The Config

```bash
./scripts/run.sh --print-config
```

This prints the resolved config without requiring the Docker stack to be
running yet.

## 4. Start The Stack

Use the published GHCR images for the fastest first run:

```bash
docker compose -f docker-compose.published.yml pull
docker compose -f docker-compose.published.yml up -d
```

To pin a specific published release, set `TANDEM_AGENTS_IMAGE_TAG`:

```bash
TANDEM_AGENTS_IMAGE_TAG=v0.5.6 docker compose -f docker-compose.published.yml up -d
```

If you want to build the images locally from this checkout instead, run:

```bash
./scripts/build-containers.sh
```

When you start with `docker-compose.published.yml`, use the same `-f` flag for
follow-up `docker compose exec`, `logs`, and `down` commands.

This starts:

- `tandem-engine`
- `tandem-control-panel`
- `tandem-kb-mcp`
- `aca`

Open the control panel at:

```text
http://127.0.0.1:39734
```

Sign in with the Tandem token printed by `./scripts/setup.sh`, or read it from:

```bash
cat tandem-data/tandem_api_token
```

After the stack is running, validate the runtime:

```bash
./scripts/run.sh --validate
```

## 5. Choose A Local Task Source

For first local testing, use the local Kanban board flow.

The checked-in `config/board.yaml` is an example board. You can edit it directly
or use the control panel Install settings to point ACA at another board file.

A minimal card looks like this:

```yaml
cards:
  - id: demo-readme-task
    title: Add a short local demo note
    lane: ready
    description: Add a short note to the demo repository README.
    acceptance_criteria:
      - README contains a local demo note
    repo:
      path: /workspace/test-repos/demo
```

For local repo testing, put a repository under `./test-repos` on the host. It is
mounted into the ACA container at `/workspace/test-repos`.

## 6. Run One Task

```bash
docker compose -f docker-compose.published.yml exec aca python3 -m src.tandem_agents.cli run
```

Watch progress:

```bash
./scripts/monitor.sh
docker compose -f docker-compose.published.yml exec aca python3 -m src.tandem_agents.cli monitor --follow
```

Run artifacts appear under:

- `runs/`
- `tandem-data/`
- `workspace/repos/`
- the selected repo/worktree path

## Optional: GitHub Projects Intake

To use GitHub Projects instead of the local board:

1. Add a PAT to `.env` as `GITHUB_PERSONAL_ACCESS_TOKEN` or place it in the file
   referenced by `GITHUB_PERSONAL_ACCESS_TOKEN_FILE`.
2. Configure GitHub Project intake in the control panel Install settings.
3. Run the same task command:

```bash
docker compose -f docker-compose.published.yml exec aca python3 -m src.tandem_agents.cli run
```

## Troubleshooting

- Docker is not running: start Docker and rerun `./scripts/build-containers.sh`.
- Port `39734` is busy: set `TANDEM_CONTROL_PANEL_PORT` in `.env`.
- Port `39735` is busy: set `ACA_API_PORT` in `.env`.
- No provider key: add the matching provider API key to `.env`.
- GitHub Project intake cannot connect: check the GitHub PAT and Project scopes.
- Tandem package resolution fails: set explicit `TANDEM_ENGINE_RELEASE_VERSION`
  and `TANDEM_CONTROL_PANEL_RELEASE_VERSION` in `.env`.
- Control panel token is unknown: run `cat tandem-data/tandem_api_token`.
