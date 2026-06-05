from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:39733"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE_NAME = "origin"
DEFAULT_STARTUP_MODE = "reuse_or_start"
DEFAULT_UPDATE_POLICY = "notify"
DEFAULT_MAX_WORKERS = 3
DEFAULT_OUTPUT_ROOT = "runs"
DEFAULT_STORAGE_PROFILE = "local"
DEFAULT_COORDINATION_BACKEND = "sqlite"
DEFAULT_COORDINATION_SQLITE_PATH = "tandem-data/coordination.sqlite3"
DEFAULT_LEASE_TTL_SECONDS = 300
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_SCHEDULER_POLICY = "fair_round_robin"
DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS = 6
DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT = 1
DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO = 1
DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT = 50
DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
DEFAULT_GITHUB_MCP_TOOLSETS = "default,projects"
DEFAULT_GITHUB_MCP_SCOPE = "intake_finalize"
DEFAULT_GITHUB_REMOTE_SYNC = "status_comment"
DEFAULT_LINEAR_MCP_SERVER = "linear"
DEFAULT_LINEAR_MCP_SCOPE = "intake_finalize"
DEFAULT_LINEAR_REMOTE_SYNC = "rich"
DEFAULT_LINEAR_CLAIM_LABEL = "aca-running"
DEFAULT_LINEAR_DONE_LABEL = "aca-done"
DEFAULT_LINEAR_BLOCKED_LABEL = "aca-blocked"
DEFAULT_EXECUTION_BACKEND = "auto"
DEFAULT_CODER_WAIT_TIMEOUT_SECONDS = 3600
DEFAULT_CODER_POLL_INTERVAL_SECONDS = 15
DEFAULT_CODER_SUPERVISOR_ENABLED = True
DEFAULT_CODER_SUPERVISOR_INTERVAL_SECONDS = 30
DEFAULT_CODER_SUPERVISOR_BATCH_SIZE = 100
DEFAULT_CODER_CANCEL_ON_SOURCE_TERMINAL = True
DEFAULT_REVIEW_POLICY = "human_review"
TASK_SOURCE_TYPES = {
    "github_project",
    "linear",
    "local_backlog",
    "manual",
    "custom",
    "kanban_board",
}
VALID_STARTUP_MODES = {"reuse_only", "reuse_or_start"}
VALID_UPDATE_POLICIES = {"notify", "block", "ignore"}
VALID_GITHUB_MCP_SCOPES = {"none", "intake_only", "intake_finalize", "always"}
VALID_GITHUB_REMOTE_SYNC = {"off", "status", "status_comment"}
VALID_LINEAR_MCP_SCOPES = {"none", "intake_only", "intake_finalize", "always"}
VALID_LINEAR_REMOTE_SYNC = {"off", "status", "status_comment", "rich"}
VALID_EXECUTION_BACKENDS = {"auto", "legacy", "coder"}
VALID_STORAGE_PROFILES = {"local", "shared"}
VALID_REVIEW_POLICIES = {"human_review", "auto_merge"}


def _nonempty(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _as_bool(value: Any, default: bool = False) -> bool:
    value = _nonempty(value)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    value = _nonempty(value)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_or_file(env: Mapping[str, str], file_env: Mapping[str, str], name: str) -> Any:
    value = _nonempty(env.get(name))
    if value is not None:
        return value
    return _nonempty(file_env.get(name))


def _resolve_path(root: Path, value: Any) -> Path | None:
    value = _nonempty(value)
    if value is None:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded or {}


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass
class RoleSelection:
    provider: str = ""
    model: str = ""


@dataclass
class AgentConfig:
    name: str = "ACA"
    dry_run: bool = False


@dataclass
class ControlPanelConfig:
    mode: str = "auto"
    aca_compact_nav: bool = True


@dataclass
class TandemConfig:
    base_url: str = DEFAULT_BASE_URL
    token_env: str = "TANDEM_API_TOKEN"
    token_file: str = ""
    required_version: str = ""
    startup_mode: str = DEFAULT_STARTUP_MODE
    update_policy: str = DEFAULT_UPDATE_POLICY
    engine_command: str = "scripts/tandem-engine-serve.sh"


@dataclass
class TaskSourceConfig:
    type: str = ""
    owner: str = ""
    repo: str = ""
    team: str = ""
    project: str = ""
    statuses: str = ""
    labels: str = ""
    query: str = ""
    item: str = ""
    url: str = ""
    path: str = ""
    prompt: str = ""
    source_name: str = ""
    card_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepositoryConfig:
    path: str = ""
    slug: str = ""
    clone_url: str = ""
    default_branch: str = DEFAULT_BRANCH
    worktree_root: str = ""
    remote_name: str = DEFAULT_REMOTE_NAME
    credential_file: str = ""


@dataclass
class ProviderConfig:
    id: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str = ""
    fallback_provider: str = ""
    fallback_model: str = ""


@dataclass
class ExecutionConfig:
    backend: str = DEFAULT_EXECUTION_BACKEND
    coder_wait_timeout_seconds: int = DEFAULT_CODER_WAIT_TIMEOUT_SECONDS
    coder_poll_interval_seconds: int = DEFAULT_CODER_POLL_INTERVAL_SECONDS
    coder_supervisor_enabled: bool = DEFAULT_CODER_SUPERVISOR_ENABLED
    coder_supervisor_interval_seconds: int = DEFAULT_CODER_SUPERVISOR_INTERVAL_SECONDS
    coder_supervisor_batch_size: int = DEFAULT_CODER_SUPERVISOR_BATCH_SIZE
    coder_cancel_on_source_terminal: bool = DEFAULT_CODER_CANCEL_ON_SOURCE_TERMINAL


@dataclass
class StorageConfig:
    profile: str = DEFAULT_STORAGE_PROFILE
    postgres_url: str = ""


@dataclass
class ReviewPolicyConfig:
    policy: str = DEFAULT_REVIEW_POLICY


@dataclass
class SwarmConfig:
    enabled: bool = False
    shared_model: bool = False
    max_workers: int = DEFAULT_MAX_WORKERS
    max_retries: int = 1
    manager: RoleSelection = field(default_factory=RoleSelection)
    worker: RoleSelection = field(default_factory=RoleSelection)
    reviewer: RoleSelection = field(default_factory=RoleSelection)
    tester: RoleSelection = field(default_factory=RoleSelection)


@dataclass
class OutputConfig:
    root: str = DEFAULT_OUTPUT_ROOT


@dataclass
class ArtifactStoreConfig:
    root: str = ""


@dataclass
class CoordinationConfig:
    backend: str = DEFAULT_COORDINATION_BACKEND
    sqlite_path: str = DEFAULT_COORDINATION_SQLITE_PATH
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    worker_id: str = ""
    host_id: str = ""


@dataclass
class SchedulerConfig:
    policy: str = DEFAULT_SCHEDULER_POLICY
    max_active_tasks: int = DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS
    max_active_tasks_per_project: int = DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_PROJECT
    max_active_tasks_per_repo: int = DEFAULT_SCHEDULER_MAX_ACTIVE_TASKS_PER_REPO
    queue_depth_limit: int = DEFAULT_SCHEDULER_QUEUE_DEPTH_LIMIT


@dataclass
class GithubMcpConfig:
    enabled: bool = False
    url: str = DEFAULT_GITHUB_MCP_URL
    toolsets: str = DEFAULT_GITHUB_MCP_TOOLSETS
    scope: str = DEFAULT_GITHUB_MCP_SCOPE
    remote_sync: str = DEFAULT_GITHUB_REMOTE_SYNC


@dataclass
class LinearMcpConfig:
    enabled: bool = False
    server: str = DEFAULT_LINEAR_MCP_SERVER
    scope: str = DEFAULT_LINEAR_MCP_SCOPE
    remote_sync: str = DEFAULT_LINEAR_REMOTE_SYNC
    claim_label: str = DEFAULT_LINEAR_CLAIM_LABEL
    done_label: str = DEFAULT_LINEAR_DONE_LABEL
    blocked_label: str = DEFAULT_LINEAR_BLOCKED_LABEL
    claim_status: str = "In Progress"
    review_status: str = "In Review"
    done_status: str = "Done"
    blocked_status: str = "Blocked"


@dataclass
class ResolvedConfig:
    root_dir: Path
    control_panel: ControlPanelConfig
    agent: AgentConfig
    tandem: TandemConfig
    task_source: TaskSourceConfig
    repository: RepositoryConfig
    provider: ProviderConfig
    execution: ExecutionConfig
    storage: StorageConfig
    review: ReviewPolicyConfig
    artifact_store: ArtifactStoreConfig
    swarm: SwarmConfig
    output: OutputConfig
    coordination: CoordinationConfig
    scheduler: SchedulerConfig
    github_mcp: GithubMcpConfig
    linear_mcp: LinearMcpConfig
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)

    def output_root(self) -> Path:
        path = Path(self.output.root).expanduser()
        if not path.is_absolute():
            path = self.root_dir / path
        return path.resolve()

    def artifact_store_root(self) -> Path:
        raw = self.artifact_store.root or str(self.output_root() / "artifact-store")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root_dir / path
        return path.resolve()

    def task_source_path(self) -> Path | None:
        return _resolve_path(self.root_dir, self.task_source.path)

    def repository_path(self) -> Path | None:
        return _resolve_path(self.root_dir, self.repository.path)

    def repository_worktree_root(self) -> Path:
        raw = self.repository.worktree_root or str(self.output_root() / "repos")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root_dir / path
        return path.resolve()

    def tandem_token_file_path(self) -> Path | None:
        return _resolve_path(
            self.root_dir,
            _nonempty(self.tandem.token_file) or _nonempty(self.env.get("TANDEM_API_TOKEN_FILE")),
        )

    def tandem_token(self) -> str:
        token_file = self.tandem_token_file_path()
        if token_file:
            token = _read_text_file(token_file)
            if token:
                return token
        return _nonempty(self.env.get(self.tandem.token_env)) or _nonempty(
            self.env.get("TANDEM_API_TOKEN")
        ) or _nonempty(self.env.get("TANDEM_TOKEN")) or ""

    def engine_host_port(self) -> tuple[str, int]:
        parsed = urlparse(self.tandem.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 39733
        return host, port

    def provider_for_role(self, role: str) -> tuple[str, str]:
        role_cfg = getattr(self.swarm, role, RoleSelection())
        provider = _nonempty(role_cfg.provider) or _nonempty(self.provider.id) or _nonempty(self.provider.fallback_provider) or DEFAULT_PROVIDER
        model = _nonempty(role_cfg.model) or _nonempty(self.provider.model) or _nonempty(self.provider.fallback_model) or DEFAULT_MODEL
        if self.swarm.shared_model:
            provider = _nonempty(self.provider.id) or _nonempty(self.provider.fallback_provider) or DEFAULT_PROVIDER
            model = _nonempty(self.provider.model) or _nonempty(self.provider.fallback_model) or DEFAULT_MODEL
        return str(provider), str(model)

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        payload.pop("env", None)
        return payload

    def config_summary(self) -> dict[str, Any]:
        return {
            "agent": self.agent.name,
            "dry_run": self.agent.dry_run,
            "tandem": {
                "base_url": self.tandem.base_url,
                "token_env": self.tandem.token_env,
                "token_file": str(self.tandem_token_file_path() or self.tandem.token_file or ""),
                "required_version": self.tandem.required_version,
                "startup_mode": self.tandem.startup_mode,
                "update_policy": self.tandem.update_policy,
                "engine_command": self.tandem.engine_command,
            },
            "task_source": _jsonable(self.task_source),
            "repository": {
                "path": str(self.repository_path() or self.repository.path or ""),
                "slug": self.repository.slug,
                "clone_url": self.repository.clone_url,
                "default_branch": self.repository.default_branch,
                "worktree_root": str(self.repository_worktree_root()),
                "remote_name": self.repository.remote_name,
                "credential_file": self.repository.credential_file,
            },
            "provider": {
                "id": self.provider.id,
                "model": self.provider.model,
                "base_url": self.provider.base_url,
                "fallback_provider": self.provider.fallback_provider,
                "fallback_model": self.provider.fallback_model,
            },
            "execution": _jsonable(self.execution),
            "storage": {
                "profile": self.storage.profile,
                "postgres_url_configured": bool(self.storage.postgres_url.strip()),
            },
            "review": _jsonable(self.review),
            "artifact_store": {
                "root": str(self.artifact_store_root()),
            },
            "swarm": {
                "enabled": self.swarm.enabled,
                "shared_model": self.swarm.shared_model,
                "max_workers": self.swarm.max_workers,
                "max_retries": self.swarm.max_retries,
                "manager": _jsonable(self.swarm.manager),
                "worker": _jsonable(self.swarm.worker),
                "reviewer": _jsonable(self.swarm.reviewer),
                "tester": _jsonable(self.swarm.tester),
            },
            "output": {"root": str(self.output_root())},
            "coordination": {
                "backend": self.coordination.backend,
                "sqlite_path": self.coordination.sqlite_path,
                "lease_ttl_seconds": self.coordination.lease_ttl_seconds,
                "heartbeat_interval_seconds": self.coordination.heartbeat_interval_seconds,
                "worker_id": self.coordination.worker_id,
                "host_id": self.coordination.host_id,
            },
            "scheduler": {
                "policy": self.scheduler.policy,
                "max_active_tasks": self.scheduler.max_active_tasks,
                "max_active_tasks_per_project": self.scheduler.max_active_tasks_per_project,
                "max_active_tasks_per_repo": self.scheduler.max_active_tasks_per_repo,
                "queue_depth_limit": self.scheduler.queue_depth_limit,
            },
            "github_mcp": _jsonable(self.github_mcp),
            "linear_mcp": _jsonable(self.linear_mcp),
            "mcp_servers": _jsonable(self.mcp_servers),
        }
