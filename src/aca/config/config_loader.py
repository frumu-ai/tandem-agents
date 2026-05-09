from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from src.aca.config.config_types import (
    ArtifactStoreConfig,
    AgentConfig,
    ControlPanelConfig,
    CoordinationConfig,
    ExecutionConfig,
    GithubMcpConfig,
    OutputConfig,
    ProviderConfig,
    RepositoryConfig,
    ResolvedConfig,
    RoleSelection,
    ReviewPolicyConfig,
    SchedulerConfig,
    StorageConfig,
    SwarmConfig,
    TandemConfig,
    TaskSourceConfig,
    DEFAULT_BASE_URL,
    DEFAULT_BRANCH,
    DEFAULT_CODER_CANCEL_ON_SOURCE_TERMINAL,
    DEFAULT_CODER_POLL_INTERVAL_SECONDS,
    DEFAULT_CODER_SUPERVISOR_BATCH_SIZE,
    DEFAULT_CODER_SUPERVISOR_ENABLED,
    DEFAULT_CODER_SUPERVISOR_INTERVAL_SECONDS,
    DEFAULT_CODER_WAIT_TIMEOUT_SECONDS,
    DEFAULT_EXECUTION_BACKEND,
    DEFAULT_GITHUB_MCP_SCOPE,
    DEFAULT_GITHUB_MCP_TOOLSETS,
    DEFAULT_GITHUB_MCP_URL,
    DEFAULT_GITHUB_REMOTE_SYNC,
    DEFAULT_MAX_WORKERS,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_COORDINATION_BACKEND,
    DEFAULT_COORDINATION_SQLITE_PATH,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS,
    DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT,
    DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO,
    DEFAULT_SCHEDULER_POLICY,
    DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT,
    DEFAULT_PROVIDER,
    DEFAULT_REMOTE_NAME,
    DEFAULT_REVIEW_POLICY,
    DEFAULT_STARTUP_MODE,
    DEFAULT_UPDATE_POLICY,
    TASK_SOURCE_TYPES,
    VALID_STARTUP_MODES,
    VALID_UPDATE_POLICIES,
    VALID_GITHUB_MCP_SCOPES,
    VALID_GITHUB_REMOTE_SYNC,
    VALID_EXECUTION_BACKENDS,
    DEFAULT_STORAGE_PROFILE,
    VALID_STORAGE_PROFILES,
    VALID_REVIEW_POLICIES,
    _as_bool,
    _as_int,
    _env_or_file,
    _load_env_file,
    _load_yaml,
    _nonempty,
    _jsonable,
)


def _load_first_existing_yaml(root_dir: Path, candidates: tuple[str, ...]) -> dict[str, Any]:
    for relative_path in candidates:
        candidate = root_dir / relative_path
        if candidate.exists():
            return _load_yaml(candidate)
    return {}


def _load_control_panel_config(root_dir: Path, env: Mapping[str, str], env_file: Mapping[str, str]) -> dict[str, Any]:
    candidate = _env_or_file(env, env_file, "TANDEM_CONTROL_PANEL_CONFIG_FILE")
    if candidate:
        path = Path(str(candidate)).expanduser()
        if not path.is_absolute():
            path = root_dir / path
        if path.exists():
            if path.suffix.lower() == ".json":
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return {}
            return _load_yaml(path)
        return {}
    fallback = root_dir / "tandem-data" / "control-panel-config.json"
    if fallback.exists():
        try:
            return json.loads(fallback.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _merge_dicts(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = deepcopy(base)
        for key, value in overlay.items():
            merged[key] = _merge_dicts(merged.get(key), value)
        return merged
    if overlay is None:
        return deepcopy(base)
    return deepcopy(overlay)


def resolve_config(root_dir: Path, env: Mapping[str, str] | None = None) -> ResolvedConfig:
    import os

    env_map = dict(os.environ)
    if env:
        env_map.update(env)
    env_file = _load_env_file(root_dir / ".env")
    merged_env = dict(env_file)
    merged_env.update(env_map)
    yaml_data = _load_first_existing_yaml(root_dir, ("agent.yaml", "config/agent.yaml"))
    control_panel_data = _load_control_panel_config(root_dir, env_map, env_file)
    data = _merge_dicts(yaml_data, control_panel_data)

    def pick(*env_names: str, yaml_value: Any = None, default: Any = None) -> Any:
        for env_name in env_names:
            value = _env_or_file(env_map, env_file, env_name)
            if value is not None:
                return value
        value = _nonempty(yaml_value)
        return default if value is None else value

    agent_data = data.get("agent", {}) or {}
    control_panel_data = data.get("control_panel", {}) or {}
    tandem_data = data.get("tandem", {}) or {}
    task_data = data.get("task_source", {}) or {}
    repo_data = data.get("repository", {}) or {}
    provider_data = data.get("provider", {}) or {}
    storage_data = data.get("storage", {}) or {}
    swarm_data = data.get("swarm", {}) or {}
    output_data = data.get("output", {}) or {}
    coordination_data = data.get("coordination", {}) or {}
    scheduler_data = data.get("scheduler", {}) or {}
    mcp_servers = dict(data.get("mcp_servers") or {})
    github_mcp_data = data.get("github_mcp", {}) or {}
    github_mcp_server = mcp_servers.get("github", {}) if isinstance(mcp_servers, dict) else {}

    def github_pat_present() -> bool:
        direct_token = _nonempty(merged_env.get("GITHUB_PERSONAL_ACCESS_TOKEN")) or _nonempty(
            merged_env.get("GITHUB_TOKEN")
        )
        if direct_token:
            return True
        for file_env_name in ("GITHUB_PERSONAL_ACCESS_TOKEN_FILE", "GITHUB_TOKEN_FILE"):
            file_value = _nonempty(merged_env.get(file_env_name))
            if not file_value:
                continue
            file_path = _resolve_path(root_dir, file_value)
            if file_path and file_path.exists() and _read_text_file(file_path):
                return True
        return False

    github_pat_available = github_pat_present()

    control_panel = ControlPanelConfig(
        mode=str(
            pick(
                "TANDEM_CONTROL_PANEL_MODE",
                yaml_value=control_panel_data.get("mode"),
                default="auto",
            )
        ),
        aca_compact_nav=_as_bool(
            pick(
                "TANDEM_CONTROL_PANEL_ACA_COMPACT_NAV",
                yaml_value=control_panel_data.get("aca_compact_nav"),
                default=True,
            )
        ),
    )
    agent = AgentConfig(
        name=str(pick("AGENT_NAME", yaml_value=agent_data.get("name"), default="ACA")),
        dry_run=_as_bool(pick("ACA_DRY_RUN", "AUTOCODER_DRY_RUN", yaml_value=agent_data.get("dry_run"), default=False)),
    )
    tandem = TandemConfig(
        base_url=str(pick("TANDEM_BASE_URL", yaml_value=tandem_data.get("base_url"), default=DEFAULT_BASE_URL)),
        token_env=str(pick("TANDEM_TOKEN_ENV", yaml_value=tandem_data.get("token_env"), default="TANDEM_API_TOKEN")),
        token_file=str(pick("TANDEM_API_TOKEN_FILE", yaml_value=tandem_data.get("token_file"), default="")),
        required_version=str(pick("TANDEM_REQUIRED_VERSION", yaml_value=tandem_data.get("required_version"), default="")),
        startup_mode=str(pick("TANDEM_STARTUP_MODE", yaml_value=tandem_data.get("startup_mode"), default=DEFAULT_STARTUP_MODE)),
        update_policy=str(pick("TANDEM_UPDATE_POLICY", yaml_value=tandem_data.get("update_policy"), default=DEFAULT_UPDATE_POLICY)),
        engine_command=str(
            pick(
                "TANDEM_ENGINE_COMMAND",
                yaml_value=tandem_data.get("engine_command"),
                default="scripts/tandem-engine-serve.sh",
            )
        ),
    )
    task_source = TaskSourceConfig(
        type=str(pick("ACA_TASK_SOURCE_TYPE", "AUTOCODER_TASK_SOURCE_TYPE", yaml_value=task_data.get("type"), default="")),
        owner=str(pick("ACA_TASK_SOURCE_OWNER", "AUTOCODER_TASK_SOURCE_OWNER", yaml_value=task_data.get("owner"), default="")),
        repo=str(pick("ACA_TASK_SOURCE_REPO", "AUTOCODER_TASK_SOURCE_REPO", yaml_value=task_data.get("repo"), default="")),
        project=str(pick("ACA_TASK_SOURCE_PROJECT", "AUTOCODER_TASK_SOURCE_PROJECT", yaml_value=task_data.get("project"), default="")),
        item=str(pick("ACA_TASK_SOURCE_ITEM", "AUTOCODER_TASK_SOURCE_ITEM", yaml_value=task_data.get("item"), default="")),
        url=str(pick("ACA_TASK_SOURCE_URL", "AUTOCODER_TASK_SOURCE_URL", yaml_value=task_data.get("url"), default="")),
        path=str(pick("ACA_TASK_SOURCE_PATH", "AUTOCODER_TASK_SOURCE_PATH", yaml_value=task_data.get("path"), default="")),
        prompt=str(pick("ACA_TASK_SOURCE_PROMPT", "AUTOCODER_TASK_SOURCE_PROMPT", yaml_value=task_data.get("prompt"), default="")),
        source_name=str(pick("ACA_TASK_SOURCE_SOURCE_NAME", "AUTOCODER_TASK_SOURCE_SOURCE_NAME", yaml_value=task_data.get("source_name"), default="")),
        card_id=str(pick("ACA_TASK_SOURCE_CARD_ID", "AUTOCODER_TASK_SOURCE_CARD_ID", yaml_value=task_data.get("card_id"), default="")),
        payload=dict(task_data.get("payload") or {}),
    )
    repo_path = pick("ACA_REPO_PATH", "AUTOCODER_REPO_PATH", yaml_value=repo_data.get("path"), default="")
    repo_slug = pick("ACA_REPO_SLUG", "AUTOCODER_REPO_SLUG", yaml_value=repo_data.get("slug"), default="")
    repo_clone_url = pick("ACA_REPO_URL", "AUTOCODER_REPO_URL", yaml_value=repo_data.get("clone_url"), default="")
    repo_default_branch = pick("ACA_DEFAULT_BRANCH", "AUTOCODER_DEFAULT_BRANCH", yaml_value=repo_data.get("default_branch"), default=DEFAULT_BRANCH)
    repo_worktree_root = pick("ACA_WORKTREE_ROOT", "AUTOCODER_WORKTREE_ROOT", yaml_value=repo_data.get("worktree_root"), default="")
    repo_remote_name = pick("ACA_REMOTE_NAME", "AUTOCODER_REMOTE_NAME", yaml_value=repo_data.get("remote_name"), default=DEFAULT_REMOTE_NAME)
    repo_credential_file = pick(
        "ACA_REPO_TOKEN_FILE",
        "GITHUB_PERSONAL_ACCESS_TOKEN_FILE",
        "GITHUB_TOKEN_FILE",
        yaml_value=repo_data.get("credential_file"),
        default="",
    )
    repository = RepositoryConfig(
        path=str(repo_path or ""),
        slug=str(repo_slug or ""),
        clone_url=str(repo_clone_url or ""),
        default_branch=str(repo_default_branch or DEFAULT_BRANCH),
        worktree_root=str(repo_worktree_root or ""),
        remote_name=str(repo_remote_name or DEFAULT_REMOTE_NAME),
        credential_file=str(repo_credential_file or ""),
    )
    provider = ProviderConfig(
        id=str(pick("ACA_PROVIDER", "AUTOCODER_PROVIDER", yaml_value=provider_data.get("id"), default=DEFAULT_PROVIDER)),
        model=str(pick("ACA_MODEL", "AUTOCODER_MODEL", yaml_value=provider_data.get("model"), default=DEFAULT_MODEL)),
        base_url=str(pick("ACA_PROVIDER_BASE_URL", "AUTOCODER_PROVIDER_BASE_URL", yaml_value=provider_data.get("base_url"), default="")),
        fallback_provider=str(pick("ACA_FALLBACK_PROVIDER", "AUTOCODER_FALLBACK_PROVIDER", yaml_value=provider_data.get("fallback_provider"), default="")),
        fallback_model=str(pick("ACA_FALLBACK_MODEL", "AUTOCODER_FALLBACK_MODEL", yaml_value=provider_data.get("fallback_model"), default="")),
    )
    storage = StorageConfig(
        profile=str(
            pick(
                "ACA_STORAGE_PROFILE",
                yaml_value=storage_data.get("profile"),
                default=DEFAULT_STORAGE_PROFILE,
            )
        ),
        postgres_url=str(
            pick(
                "ACA_COORDINATION_POSTGRES_URL",
                yaml_value=storage_data.get("postgres_url"),
                default="",
            )
        ),
    )
    coordination_backend_default = "postgres" if storage.profile == "shared" else DEFAULT_COORDINATION_BACKEND
    execution = ExecutionConfig(
        backend=str(
            pick(
                "ACA_EXECUTION_BACKEND",
                yaml_value=data.get("execution", {}).get("backend"),
                default=DEFAULT_EXECUTION_BACKEND,
            )
        ),
        coder_wait_timeout_seconds=max(
            1,
            _as_int(
                pick(
                    "ACA_CODER_WAIT_TIMEOUT_SECONDS",
                    yaml_value=data.get("execution", {}).get("coder_wait_timeout_seconds"),
                    default=DEFAULT_CODER_WAIT_TIMEOUT_SECONDS,
                ),
                DEFAULT_CODER_WAIT_TIMEOUT_SECONDS,
            ),
        ),
        coder_poll_interval_seconds=max(
            1,
            _as_int(
                pick(
                    "ACA_CODER_POLL_INTERVAL_SECONDS",
                    yaml_value=data.get("execution", {}).get("coder_poll_interval_seconds"),
                    default=DEFAULT_CODER_POLL_INTERVAL_SECONDS,
                ),
                DEFAULT_CODER_POLL_INTERVAL_SECONDS,
            ),
        ),
        coder_supervisor_enabled=_as_bool(
            pick(
                "ACA_CODER_SUPERVISOR_ENABLED",
                yaml_value=data.get("execution", {}).get("coder_supervisor_enabled"),
                default=DEFAULT_CODER_SUPERVISOR_ENABLED,
            )
        ),
        coder_supervisor_interval_seconds=max(
            1,
            _as_int(
                pick(
                    "ACA_CODER_SUPERVISOR_INTERVAL_SECONDS",
                    yaml_value=data.get("execution", {}).get("coder_supervisor_interval_seconds"),
                    default=DEFAULT_CODER_SUPERVISOR_INTERVAL_SECONDS,
                ),
                DEFAULT_CODER_SUPERVISOR_INTERVAL_SECONDS,
            ),
        ),
        coder_supervisor_batch_size=max(
            1,
            _as_int(
                pick(
                    "ACA_CODER_SUPERVISOR_BATCH_SIZE",
                    yaml_value=data.get("execution", {}).get("coder_supervisor_batch_size"),
                    default=DEFAULT_CODER_SUPERVISOR_BATCH_SIZE,
                ),
                DEFAULT_CODER_SUPERVISOR_BATCH_SIZE,
            ),
        ),
        coder_cancel_on_source_terminal=_as_bool(
            pick(
                "ACA_CODER_CANCEL_ON_SOURCE_TERMINAL",
                yaml_value=data.get("execution", {}).get("coder_cancel_on_source_terminal"),
                default=DEFAULT_CODER_CANCEL_ON_SOURCE_TERMINAL,
            )
        ),
    )
    review = ReviewPolicyConfig(
        policy=str(
            pick(
                "ACA_REVIEW_POLICY",
                yaml_value=data.get("review", {}).get("policy"),
                default=DEFAULT_REVIEW_POLICY,
            )
        )
    )
    swarm = SwarmConfig(
        enabled=_as_bool(pick("ACA_ENABLE_SWARM", "AUTOCODER_ENABLE_SWARM", yaml_value=swarm_data.get("enabled"), default=False)),
        shared_model=_as_bool(pick("ACA_SHARED_MODEL", "AUTOCODER_SHARED_MODEL", yaml_value=swarm_data.get("shared_model"), default=False)),
        max_workers=max(1, _as_int(pick("ACA_MAX_WORKERS", "AUTOCODER_MAX_WORKERS", yaml_value=swarm_data.get("max_workers"), default=DEFAULT_MAX_WORKERS), DEFAULT_MAX_WORKERS)),
        manager=RoleSelection(
            provider=str(pick("ACA_MANAGER_PROVIDER", "AUTOCODER_MANAGER_PROVIDER", yaml_value=(swarm_data.get("manager") or {}).get("provider"), default="")),
            model=str(pick("ACA_MANAGER_MODEL", "AUTOCODER_MANAGER_MODEL", yaml_value=(swarm_data.get("manager") or {}).get("model"), default="")),
        ),
        worker=RoleSelection(
            provider=str(pick("ACA_WORKER_PROVIDER", "AUTOCODER_WORKER_PROVIDER", yaml_value=(swarm_data.get("worker") or {}).get("provider"), default="")),
            model=str(pick("ACA_WORKER_MODEL", "AUTOCODER_WORKER_MODEL", yaml_value=(swarm_data.get("worker") or {}).get("model"), default="")),
        ),
        reviewer=RoleSelection(
            provider=str(pick("ACA_REVIEWER_PROVIDER", "AUTOCODER_REVIEWER_PROVIDER", yaml_value=(swarm_data.get("reviewer") or {}).get("provider"), default="")),
            model=str(pick("ACA_REVIEWER_MODEL", "AUTOCODER_REVIEWER_MODEL", yaml_value=(swarm_data.get("reviewer") or {}).get("model"), default="")),
        ),
        tester=RoleSelection(
            provider=str(pick("ACA_TESTER_PROVIDER", "AUTOCODER_TESTER_PROVIDER", yaml_value=(swarm_data.get("tester") or {}).get("provider"), default="")),
            model=str(pick("ACA_TESTER_MODEL", "AUTOCODER_TESTER_MODEL", yaml_value=(swarm_data.get("tester") or {}).get("model"), default="")),
        ),
    )
    output_root = pick("ACA_OUTPUT_ROOT", "AUTOCODER_OUTPUT_ROOT", yaml_value=output_data.get("root"), default=DEFAULT_OUTPUT_ROOT)
    output = OutputConfig(root=str(output_root or DEFAULT_OUTPUT_ROOT))
    artifact_store = ArtifactStoreConfig(
        root=str(
            pick(
                "ACA_ARTIFACT_STORE_ROOT",
                yaml_value=data.get("artifact_store", {}).get("root"),
                default="",
            )
        )
    )
    coordination = CoordinationConfig(
        backend=str(
            pick(
                "ACA_COORDINATION_BACKEND",
                yaml_value=coordination_data.get("backend"),
                default=coordination_backend_default,
            )
        ),
        sqlite_path=str(
            pick(
                "ACA_COORDINATION_SQLITE_PATH",
                yaml_value=coordination_data.get("sqlite_path"),
                default=DEFAULT_COORDINATION_SQLITE_PATH,
            )
        ),
        lease_ttl_seconds=max(
            1,
            _as_int(
                pick(
                    "ACA_LEASE_TTL_SECONDS",
                    yaml_value=coordination_data.get("lease_ttl_seconds"),
                    default=DEFAULT_LEASE_TTL_SECONDS,
                ),
                DEFAULT_LEASE_TTL_SECONDS,
            ),
        ),
        heartbeat_interval_seconds=max(
            1,
            _as_int(
                pick(
                    "ACA_HEARTBEAT_INTERVAL_SECONDS",
                    yaml_value=coordination_data.get("heartbeat_interval_seconds"),
                    default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                ),
                DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            ),
        ),
        worker_id=str(
            pick(
                "ACA_WORKER_ID",
                yaml_value=coordination_data.get("worker_id"),
                default="",
            )
        ),
        host_id=str(
            pick(
                "ACA_HOST_ID",
                yaml_value=coordination_data.get("host_id"),
                default="",
            )
        ),
    )
    scheduler = SchedulerConfig(
        policy=str(
            pick(
                "ACA_SCHEDULER_POLICY",
                yaml_value=scheduler_data.get("policy"),
                default=DEFAULT_SCHEDULER_POLICY,
            )
        ),
        max_active_tasks=max(
            1,
            _as_int(
                pick(
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS",
                    yaml_value=scheduler_data.get("max_active_tasks"),
                    default=DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS,
                ),
                DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS,
            ),
        ),
        max_active_tasks_per_project=max(
            1,
            _as_int(
                pick(
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT",
                    yaml_value=scheduler_data.get("max_active_tasks_per_project"),
                    default=DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT,
                ),
                DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT,
            ),
        ),
        max_active_tasks_per_repo=max(
            1,
            _as_int(
                pick(
                    "ACA_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO",
                    yaml_value=scheduler_data.get("max_active_tasks_per_repo"),
                    default=DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO,
                ),
                DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO,
            ),
        ),
        queue_depth_limit=max(
            1,
            _as_int(
                pick(
                    "ACA_SCHEDULER_QUEUE_DEPTH_LIMIT",
                    yaml_value=scheduler_data.get("queue_depth_limit"),
                    default=DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT,
                ),
                DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT,
            ),
        ),
    )
    github_enabled_value = _nonempty(github_mcp_data.get("enabled", github_mcp_server.get("enabled")))
    if github_enabled_value is None:
        github_enabled = github_pat_available
    else:
        github_enabled = _as_bool(github_enabled_value, default=False)
    github_mcp = GithubMcpConfig(
        enabled=github_enabled,
        url=str(
            pick(
                "ACA_GITHUB_MCP_URL",
                yaml_value=github_mcp_data.get("url", github_mcp_server.get("transport")),
                default=DEFAULT_GITHUB_MCP_URL,
            )
        ),
        toolsets=str(
            pick(
                "ACA_GITHUB_MCP_TOOLSETS",
                yaml_value=github_mcp_data.get("toolsets", github_mcp_server.get("headers", {}).get("X-MCP-Toolsets")),
                default=DEFAULT_GITHUB_MCP_TOOLSETS,
            )
        ),
        scope=str(
            pick(
                "ACA_GITHUB_MCP_SCOPE",
                yaml_value=github_mcp_data.get("scope", github_mcp_server.get("scope")),
                default=DEFAULT_GITHUB_MCP_SCOPE,
            )
        ),
        remote_sync=str(
            pick(
                "ACA_GITHUB_REMOTE_SYNC",
                yaml_value=github_mcp_data.get("remote_sync", github_mcp_server.get("remote_sync")),
                default=DEFAULT_GITHUB_REMOTE_SYNC,
            )
        ),
    )
    if not mcp_servers and github_mcp.enabled:
        mcp_servers = {
            "github": {
                "enabled": github_mcp.enabled,
                "transport": github_mcp.url,
                "headers": {"X-MCP-Toolsets": github_mcp.toolsets} if github_mcp.toolsets else {},
                "auth": {
                    "token_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
                    "token_file_envs": ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE", "GITHUB_TOKEN_FILE"],
                },
                "auto_connect": True,
                "scope": github_mcp.scope,
                "remote_sync": github_mcp.remote_sync,
            }
        }
    return ResolvedConfig(
        root_dir=root_dir,
        control_panel=control_panel,
        agent=agent,
        tandem=tandem,
        task_source=task_source,
        repository=repository,
        provider=provider,
        execution=execution,
        storage=storage,
        review=review,
        artifact_store=artifact_store,
        swarm=swarm,
        output=output,
        coordination=coordination,
        scheduler=scheduler,
        github_mcp=github_mcp,
        mcp_servers=mcp_servers,
        env=merged_env,
    )


def validate_config(cfg: ResolvedConfig) -> list[str]:
    errors: list[str] = []
    if cfg.task_source.type not in TASK_SOURCE_TYPES:
        errors.append(
            f"Unsupported task source type: {cfg.task_source.type or '<missing>'}. "
            f"Expected one of: {', '.join(sorted(TASK_SOURCE_TYPES))}."
        )
    if not (cfg.repository.path or cfg.repository.slug or cfg.repository.clone_url):
        errors.append("Repository binding is required: set ACA_REPO_PATH, ACA_REPO_SLUG, or ACA_REPO_URL.")
    if not cfg.provider.id or not cfg.provider.model:
        errors.append("Provider and model are required.")
    if cfg.tandem.startup_mode not in VALID_STARTUP_MODES:
        errors.append(f"Invalid tandem.startup_mode: {cfg.tandem.startup_mode}")
    if cfg.tandem.update_policy not in VALID_UPDATE_POLICIES:
        errors.append(f"Invalid tandem.update_policy: {cfg.tandem.update_policy}")
    if cfg.github_mcp.scope not in VALID_GITHUB_MCP_SCOPES:
        errors.append(f"Invalid github_mcp.scope: {cfg.github_mcp.scope}")
    if cfg.github_mcp.remote_sync not in VALID_GITHUB_REMOTE_SYNC:
        errors.append(f"Invalid github_mcp.remote_sync: {cfg.github_mcp.remote_sync}")
    if cfg.execution.backend not in VALID_EXECUTION_BACKENDS:
        errors.append(f"Invalid execution.backend: {cfg.execution.backend}")
    if cfg.review.policy not in VALID_REVIEW_POLICIES:
        errors.append(
            f"Invalid review.policy: {cfg.review.policy}. "
            f"Expected one of: {', '.join(sorted(VALID_REVIEW_POLICIES))}."
        )
    if cfg.storage.profile not in VALID_STORAGE_PROFILES:
        errors.append(
            f"Invalid storage.profile: {cfg.storage.profile}. "
            f"Expected one of: {', '.join(sorted(VALID_STORAGE_PROFILES))}."
        )
    if cfg.swarm.max_workers < 1:
        errors.append("swarm.max_workers must be at least 1.")
    if cfg.coordination.backend not in {"sqlite", "postgres"}:
        errors.append(f"Invalid coordination.backend: {cfg.coordination.backend}")
    if cfg.storage.profile == "shared":
        if not cfg.storage.postgres_url:
            errors.append("storage.profile=shared requires ACA_COORDINATION_POSTGRES_URL or storage.postgres_url.")
        if cfg.coordination.backend != "postgres":
            errors.append("storage.profile=shared requires coordination.backend=postgres.")
    if cfg.review.policy == "auto_merge":
        errors.append("review.policy=auto_merge is not implemented yet; human_review is the only supported policy.")
    if cfg.coordination.heartbeat_interval_seconds <= 0:
        errors.append("coordination.heartbeat_interval_seconds must be positive.")
    if cfg.coordination.lease_ttl_seconds <= 0:
        errors.append("coordination.lease_ttl_seconds must be positive.")
    if cfg.coordination.heartbeat_interval_seconds > cfg.coordination.lease_ttl_seconds:
        errors.append("coordination.heartbeat_interval_seconds must not exceed coordination.lease_ttl_seconds.")
    elif cfg.coordination.heartbeat_interval_seconds * 3 > cfg.coordination.lease_ttl_seconds:
        # Need at least three heartbeat attempts inside one TTL window so a
        # single dropped heartbeat (transient DB/network blip) does not cause
        # spurious lease expiration. heartbeat_interval * 3 <= lease_ttl is
        # the minimum safe ratio; anything tighter is strictly informational.
        errors.append(
            "coordination.heartbeat_interval_seconds * 3 must be <= "
            "coordination.lease_ttl_seconds (need at least three heartbeats per "
            "TTL window to tolerate a transient miss; current: "
            f"heartbeat={cfg.coordination.heartbeat_interval_seconds}s, "
            f"lease_ttl={cfg.coordination.lease_ttl_seconds}s)."
        )
    if cfg.scheduler.policy not in {"fair_round_robin"}:
        errors.append(f"Invalid scheduler.policy: {cfg.scheduler.policy}")
    if cfg.scheduler.max_active_tasks < 1:
        errors.append("scheduler.max_active_tasks must be at least 1.")
    if cfg.scheduler.max_active_tasks_per_project < 1:
        errors.append("scheduler.max_active_tasks_per_project must be at least 1.")
    if cfg.scheduler.max_active_tasks_per_repo < 1:
        errors.append("scheduler.max_active_tasks_per_repo must be at least 1.")
    if cfg.scheduler.queue_depth_limit < 1:
        errors.append("scheduler.queue_depth_limit must be at least 1.")
    if cfg.execution.coder_wait_timeout_seconds <= 0:
        errors.append("execution.coder_wait_timeout_seconds must be positive.")
    role_values = [
        cfg.swarm.manager,
        cfg.swarm.worker,
        cfg.swarm.reviewer,
        cfg.swarm.tester,
    ]
    if cfg.swarm.shared_model:
        for role in role_values:
            if role.provider or role.model:
                errors.append("Role-specific swarm overrides must be empty when swarm.shared_model is true.")
                break
    if cfg.task_source.type == "manual" and not cfg.task_source.prompt:
        errors.append("Manual task source requires task_source.prompt.")
    if cfg.task_source.type == "local_backlog" and not cfg.task_source.path:
        errors.append("Local backlog task source requires task_source.path.")
    if cfg.task_source.type == "kanban_board" and not cfg.task_source.path:
        errors.append("Kanban board task source requires task_source.path.")
    if cfg.task_source.type == "github_project" and not (cfg.task_source.owner and cfg.task_source.project):
        errors.append("GitHub project task source requires task_source.owner and task_source.project.")
    if cfg.task_source.type == "custom" and not (cfg.task_source.source_name and cfg.task_source.payload):
        errors.append("Custom task source requires task_source.source_name and task_source.payload.")
    if cfg.provider.fallback_model and not cfg.provider.fallback_provider:
        errors.append("provider.fallback_model requires provider.fallback_provider.")
    return errors


def print_json(data: Any) -> str:
    return json.dumps(_jsonable(data), indent=2, sort_keys=False)
