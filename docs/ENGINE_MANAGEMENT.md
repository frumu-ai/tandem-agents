# ACA Tandem Engine Management

This document defines how ACA should find, reuse, start, and report on the Tandem engine it depends on.

## Purpose

ACA should not blindly install or restart Tandem every time it runs.

Instead, it should:

- prefer an already-running Tandem engine when one is available
- reuse a newer engine instead of downgrading it
- notify the operator when the running engine is older than the configured requirement
- surface update availability in the run record so the operator can see it later
- only start a local engine when the config allows it and no compatible engine is reachable

## Core Policy

The preferred startup order is:

1. check whether the configured `tandem.base_url` is reachable
2. if reachable, query the engine version and reuse it
3. if not reachable, start the configured engine command only when startup mode allows it
4. if the engine version is newer than the configured minimum, keep using it and notify
5. if the engine version is older than the configured minimum, block or warn according to policy

## Recommended Config Fields

The canonical config in `CONFIG_SCHEMA.md` should include a `tandem` section with fields like:

- `base_url`: engine URL to check first
- `token_file`: mounted secret file that holds the Tandem token, preferably `tandem-data/tandem_api_token` for Compose or `/run/secrets/tandem_api_token` for custom secret mounts
- `token_env`: env var that holds the Tandem token as a legacy fallback, preferably `TANDEM_API_TOKEN`
- `required_version`: minimum acceptable Tandem engine version
- `startup_mode`: `reuse_only` or `reuse_or_start`
- `update_policy`: `notify`, `block`, or `ignore`
- `engine_command`: command used to start a local Tandem engine when allowed

## Version Handling

Version checks should be conservative and fail-closed.

- If the engine is already running and satisfies the requirement, reuse it.
- If the engine is already running and is newer than the required minimum, do not replace it.
- If the engine is running but older than required, surface a clear warning or blocker.
- If no engine is running and startup is allowed, start the configured command and record the version that comes up.

## Update Notification

ACA should tell the operator when it detects a mismatch between the configured requirement and the running engine.

The notification should include:

- the configured base URL
- the running engine version, if known
- the required or expected version, if configured
- whether ACA reused the engine, warned, blocked, or started a new process

That notification should also be written into the run state so `scripts/monitor.sh` can show it later.

## Runtime Expectations

The Tandem engine is an external dependency, not something ACA owns internally.

That means ACA should:

- verify the engine before task execution begins
- record the engine details in `status.json`
- avoid launching duplicate engines when one is already healthy
- keep the update decision visible to the operator

When ACA runs in Docker Compose, the preferred topology is:

- a dedicated `tandem-engine` sidecar
- a dedicated `tandem-control-panel` companion UI that points at that sidecar
- a persistent engine-state mount at `./tandem-engine-state`
- a shared Tandem output mount at `./tandem-data`
- a mounted token file at `./tandem-data/tandem_api_token` that appears inside ACA containers as `/workspace/tandem-data/tandem_api_token`
- an internal sidecar port of `39733` by default so it does not collide with a host Tandem at `39731`
- a control-panel host port of `39734` by default so the UI and ACA always target the same engine in Compose
- ACA configured with `TANDEM_ENGINE_STARTUP_MODE=reuse_only`
- the Tandem API token loaded from the mounted secret file and materialized as `TANDEM_API_TOKEN` only inside the engine wrapper process

If the secret file is missing on first start, the engine wrapper may create it automatically before serving requests.

That keeps the runtime deterministic while still allowing ACA to attach to a standalone engine when `TANDEM_BASE_URL` points somewhere else.

## Relation To The Rest Of ACA

- `CONFIG_SCHEMA.md` defines the config knobs
- `RUN_STATE_SCHEMA.md` defines how engine status is recorded
- `MONITORING.md` defines how the operator sees the current engine state
- `V0_1_BUILD_PLAN.md` defines this as part of the first runnable release
