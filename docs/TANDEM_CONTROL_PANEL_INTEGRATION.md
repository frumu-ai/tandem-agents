# Tandem Control Panel Integration

This document describes how the Tandem Control Panel connects to and manages the ACA (Autonomous Coding Agent) control plane.

## Overview

ACA provides a FastAPI-based REST API and SSE (Server-Sent Events) stream. The Tandem Control Panel acts as a client to this API, allowing users to manage multiple coding projects and monitor autonomous runs in real-time.

## Connection Mechanism

### Discovery & Authentication
Since both the Control Panel and ACA typically run within the same Docker Compose stack or on the same host:

1.  **API URL**: The Control Panel should default to `http://localhost:39735` (or the internal Docker service name `http://aca:39735`).
2.  **Token Discovery**: ACA stores its `ACA_API_TOKEN` in the `.env` file or can be configured to write it to a shared secret volume. The Control Panel can auto-discover this token if it has access to the environment or the shared secret path.
3.  **Authentication**: All requests to ACA must include the header `Authorization: Bearer <ACA_API_TOKEN>`.

### Connection State
The Control Panel should check the `/health` endpoint of ACA.
- **Connected**: ACA is reachable and healthy. Show the "Coding" dashboard.
- **Disconnected**: ACA is unreachable. The Control Panel should display a message in the coding section: *"ACA integration required. Please ensure the ACA service is running to enable autonomous coding features."*

## User Interface Requirements

To support the multi-project and concurrent execution capabilities of the ACA API, the Tandem Control Panel should implement the following views:

### 1. Project Selector
Users must be able to switch between different git repository contexts.
- **Project List**: Fetch via `GET /projects`. Show a list or dropdown of registered repositories.
- **Active Context**: Selecting a project should update the "Task Intake" and "Run History" views to focus on that specific repository.
- **Add Project**: A dedicated UI flow to call `POST /projects` with a new slug and git URL.

### 2. Multi-Run Dashboard (Global Overview)
A high-level view that aggregates activity across all repositories.
- **Active Repositories**: Show a card for every project that currently has one or more active runs.
- **Live Status Summary**: For each active run, display:
    - The repository slug and current branch.
    - The active phase (e.g., `Manager Planning`, `Worker Execution`).
    - A progress bar or metric (e.g., `3/5 Subtasks Completed`).
    - The most recent event from the SSE stream.
- **Navigation**: Clicking a run card should drill down into the granular **Run Detail** view (Logs + Blackboard).

### 3. Run Detail View (Per-Project)
- **Blackboard Visualization**: Render the orchestration tree (Manager -> Subtasks -> Workers) fetched from `GET /runs/{run_id}`.
- **Live Logs**: Provide a terminal-like window that tails logs from `GET /runs/{run_id}/logs/{log_name}`.
- **Artifacts**: Display links to generated code, diffs, and summaries once the run enters the `completed` state.

## Control Capabilities

The Control Panel can trigger the following actions via the ACA API:

- **Project Registration**: `POST /projects` to bind new git repositories.
- **Task Intake**: `GET /projects/{slug}/tasks` to preview available cards from Kanban boards or GitHub Projects.
- **Trigger Runs**: `POST /runs/trigger` to start a new autonomous coding session for a specific project.
- **Run Overrides**: Pass configuration overrides (e.g., specific LLM models) per run.

## Monitoring Capabilities

### Real-time Events (SSE)
The Control Panel should subscribe to SSE streams for a "Live" feel:
- **Global Stream (`/events`)**: Listen for system-wide status changes and new run starts.
- **Run Stream (`/runs/{run_id}/events`)**: Listen for granular orchestration events:
    - `manager.started`: Manager is planning subtasks.
    - `swarm.spawned`: Workers are being initialized.
    - `worker.started`: A specific worker has begun its task.
    - `worker.artifact_captured`: A new screenshot or file is available.
        - Payload: `{"worker_id": "...", "artifact_type": "screenshot", "url": "/runs/{run_id}/artifacts/{name}"}`
    - `run.completed`: The entire mission is finished.

### Live Visual Monitoring
When a `worker.artifact_captured` event of type `screenshot` is received, the Control Panel should display the image in a "Live Feed" or "Visual Audit" section of the Run Detail view. This allows users to follow along as the agent tests the UI in real-time.

### Log Streaming
For deep inspection, the Control Panel can fetch and tail logs:
- `GET /runs/{run_id}/logs`: List active worker log files.
- `GET /runs/{run_id}/logs/{log_name}`: Fetch the tail of a specific log to display in a terminal-like view.

### State Visualization
- **Blackboard**: `GET /runs/{run_id}` returns the "Blackboard," which contains the manager's plan, subtask statuses, and worker results. Use this to render the orchestration tree (Manager -> Subtasks -> Workers).

## Implementation Notes for Control Panel

- **Concurrency**: The ACA API supports multiple parallel runs. The Control Panel should support a "Multi-task" view showing several active runs simultaneously.
- **Repository Isolation**: ACA handles the cloning and worktree management. The Control Panel only needs to track the `project_slug`.
- **Handoff**: When a run completes, ACA writes a `summary.md` and artifacts. The Control Panel should display these as the final "Result" of the coding session.
