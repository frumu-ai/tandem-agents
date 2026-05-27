# ACA Commands

This document collects the commands you can use to inspect ACA and trigger the main operator actions.

## Check What Is Happening

Use these commands when you want the current state.

If Tandem is already running on the host, that instance may still be on `39731`.
ACA Docker Compose now exposes its own control panel on `39734` and keeps the ACA-managed engine on internal port `39733`.
If you also have a separate host or Tandem-repo control panel running, keep the ports distinct so you know which stack you are talking to.

Use the host Tandem commands when you want to inspect the server instance.
Use the Compose-sidecar command when you want the ACA-managed engine.

```bash
./scripts/setup.sh
./scripts/monitor.sh
./scripts/monitor.sh --follow
./scripts/monitor.sh --run-dir ~/tandem-agents/runs/<run_id>
```

```bash
./scripts/run.sh --setup
./scripts/run.sh --print-config
./scripts/run.sh --validate
./scripts/run.sh --check-engine
```

```bash
tandem-engine status
curl -s http://127.0.0.1:39731/global/health | jq .
docker compose exec tandem-engine tandem-engine status
```

## Trigger Things

Use these commands when you want to start or refresh something:

```bash
./scripts/run.sh --dry-run
./scripts/run.sh
./scripts/run.sh worker
./scripts/run.sh outbox-dispatcher
./scripts/run.sh scheduler-plan
./scripts/run.sh scheduler-dispatch
./scripts/run.sh workspace
./scripts/run.sh operator-status
./scripts/run.sh --init-board
./scripts/run.sh coordination-status
./scripts/build-containers.sh
docker compose logs -f aca
docker compose logs -f tandem-engine
docker compose logs -f tandem-control-panel
```

`./scripts/run.sh` and `./scripts/build-containers.sh` will auto-bootstrap the Tandem token file on first start if it is missing.
`./scripts/build-containers.sh` resolves the latest Tandem engine and control panel releases independently by default. Local builds install `@frumu/tandem`; hosted builds can set `TANDEM_ENGINE_PACKAGE=@frumu/tandem-enterprise`. Set `TANDEM_ENGINE_RELEASE_VERSION` or `TANDEM_CONTROL_PANEL_RELEASE_VERSION` if you want to lock one package. `TANDEM_RELEASE_VERSION` still locks both for backwards compatibility.
`./scripts/run.sh coordination-status` prints the durable coordination ledger for tasks, leases, workers, and outbox events.
`./scripts/run.sh worker` starts the explicit worker-mode entrypoint and tags the run as worker-coordination identity.
`./scripts/run.sh outbox-dispatcher` starts the standalone GitHub outbox drain loop.
`./scripts/run.sh scheduler-plan` prints the scheduler admission snapshot and the currently admissible task batch.
`./scripts/run.sh scheduler-dispatch` starts the admitted batch in parallel using worker mode and the scheduler plan.
`./scripts/run.sh workspace` prints the saved workspace registry from `.tandem-agents/workspace.yaml` and can set the active project with `--set-active <project_id>`.
`./scripts/run.sh operator-status` prints a joined operator view across tasks, leases, workers, runs, branch / PR links, and recovery state.
`curl -s -H "Authorization: Bearer <token>" "http://127.0.0.1:39735/dashboard"` renders the compact operator dashboard HTML view.
`./scripts/setup.sh` is the recommended first-run path because it bootstraps token files and writes `tandem-data/control-panel-config.json`.
Use the control panel Install settings to manage repo binding, task source, provider/model, and swarm policy after boot.
The setup output prints the Tandem token value so you can sign in to the control panel on first boot.
For GitHub Projects, set a GitHub PAT in `.env` or mount it through `GITHUB_PERSONAL_ACCESS_TOKEN_FILE` so Tandem can auto-bootstrap the official GitHub MCP server. `GITHUB_PERSONAL_ACCESS_TOKEN` is preferred, with `GITHUB_TOKEN` as a fallback.
The hosted control panel should prompt for that PAT during the Integrations or GitHub Projects setup flow, then persist it to the secret file so the engine can reconnect on restart without re-prompting.

```bash
tandem-engine run "Summarize the current repository state"
tandem-engine tool --json '{"tool":"workspace_list_files","args":{"path":"."}}'
```

## Engine Commands

These come from Tandem itself and are useful when ACA is attaching to a live engine:

```bash
tandem-engine --help
tandem-engine status
tandem-engine token generate
tandem-engine token generate > tandem-data/tandem_api_token
chmod 600 tandem-data/tandem_api_token
tandem-engine serve --hostname 127.0.0.1 --port 39733
tandem-engine run "Check workspace readiness"
tandem-engine providers
```

For ACA in Docker Compose, `docker compose exec tandem-engine tandem-engine status` checks the internal sidecar directly, and the sidecar defaults to `TANDEM_PORT=39733` so it stays separate from a host Tandem running on `39731` and a control-panel service running on `39732`.
ACA's bundled control panel defaults to `http://127.0.0.1:39734` and is wired to that same `tandem-engine` sidecar.

`tandem-engine token generate > tandem-data/tandem_api_token` is usually only needed if you want to pre-seed the secret file manually. ACA can also generate the file automatically on first engine startup.

## Common Operator Loops

### Inspect

1. Run `./scripts/monitor.sh`.
2. If you need live updates, run `./scripts/monitor.sh --follow`.
3. If the engine looks wrong, run `./scripts/run.sh --check-engine`.

### Validate

1. Run `./scripts/run.sh --validate`.
2. If you have not configured ACA yet, run `./scripts/setup.sh`.
3. If you want to see the resolved settings, run `./scripts/run.sh --print-config`.
4. If the repo binding or engine check fails, fix the config before starting work.

### Trigger

1. Run `./scripts/build-containers.sh` when you want the containerized flow to start.
2. Run `tandem-engine run "..."` when you want to trigger a direct Tandem prompt.
3. Run `./scripts/run.sh` once you want ACA to claim a board card and execute it.

### Trigger From The ACA API

Use the FastAPI endpoints when you want the control panel or a local script to trigger specific tasks.

Inspect the scheduler admission plan:

```bash
curl -s "http://127.0.0.1:39735/scheduler/plan" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" | jq .
```

Dispatch the currently admitted batch:

```bash
curl -s -X POST "http://127.0.0.1:39735/scheduler/dispatch?wait=false" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" | jq .
```

Inspect the workspace registry:

```bash
curl -s "http://127.0.0.1:39735/workspace" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" | jq .
```

Set the active project binding:

```bash
curl -s -X POST "http://127.0.0.1:39735/workspace/active/<project_id>" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" | jq .
```

Trigger one task for a registered project:

```bash
curl -s -X POST "http://127.0.0.1:39735/runs/trigger?project_slug=<slug>&item=<project_item_id>" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" \
  -H "Content-Type: application/json" \
  -d '{"ACA_PROVIDER":"openrouter","ACA_MODEL":"minimax/minimax-m2.7"}'
```

Trigger several tasks at once. This creates one ACA run per item and starts them immediately:

```bash
curl -s -X POST "http://127.0.0.1:39735/runs/trigger-batch" \
  -H "Authorization: Bearer $(cat tandem-data/aca_api_token)" \
  -H "Content-Type: application/json" \
  -d '{
    "project_slug":"<slug>",
    "items":["12345","12346","12347"],
    "overrides":{"ACA_PROVIDER":"openrouter","ACA_MODEL":"minimax/minimax-m2.7"}
  }'
```
