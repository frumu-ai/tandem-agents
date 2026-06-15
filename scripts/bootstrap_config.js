#!/usr/bin/env node
const { existsSync, mkdirSync, readFileSync, writeFileSync, chmodSync } = require("fs");
const { dirname, resolve } = require("path");
const { randomBytes } = require("crypto");

const ROOT_DIR = resolve(__dirname, "..");
const ENV_EXAMPLE = resolve(ROOT_DIR, ".env.example");
const ENV_FILE = resolve(ROOT_DIR, ".env");
const AGENT_YAML = resolve(ROOT_DIR, "config", "agent.yaml");
const CONTROL_PANEL_CONFIG_FILE = resolve(ROOT_DIR, "tandem-data", "control-panel-config.json");
const ACA_TOKEN_FILE = resolve(ROOT_DIR, "tandem-data", "aca_api_token");
const TANDEM_TOKEN_FILE = resolve(ROOT_DIR, "tandem-data", "tandem_api_token");
const BOARD_YAML = resolve(ROOT_DIR, "config", "board.yaml");

const MOVED_ENV_KEYS = new Set([
  "AGENT_NAME",
  "ACA_DRY_RUN",
  "ACA_API_TOKEN",
  "ACA_API_TOKEN_FILE",
  "TANDEM_API_TOKEN",
  "TANDEM_API_TOKEN_FILE",
  "TANDEM_CONTROL_PANEL_ENGINE_TOKEN",
  "ACA_REPO_PATH",
  "ACA_REPO_SLUG",
  "ACA_REPO_URL",
  "ACA_DEFAULT_BRANCH",
  "ACA_WORKTREE_ROOT",
  "ACA_REMOTE_NAME",
  "ACA_TASK_SOURCE_TYPE",
  "ACA_TASK_SOURCE_OWNER",
  "ACA_TASK_SOURCE_REPO",
  "ACA_TASK_SOURCE_TEAM",
  "ACA_TASK_SOURCE_PROJECT",
  "ACA_TASK_SOURCE_STATUSES",
  "ACA_TASK_SOURCE_LABELS",
  "ACA_TASK_SOURCE_QUERY",
  "ACA_TASK_SOURCE_ITEM",
  "ACA_TASK_SOURCE_URL",
  "ACA_TASK_SOURCE_PATH",
  "ACA_TASK_SOURCE_PROMPT",
  "ACA_TASK_SOURCE_SOURCE_NAME",
  "ACA_TASK_SOURCE_CARD_ID",
  "ACA_PROVIDER",
  "ACA_MODEL",
  "ACA_PROVIDER_BASE_URL",
  "ACA_FALLBACK_PROVIDER",
  "ACA_FALLBACK_MODEL",
  "ACA_EXECUTION_BACKEND",
  "ACA_CODER_WAIT_TIMEOUT_SECONDS",
  "ACA_CODER_POLL_INTERVAL_SECONDS",
  "ACA_CODER_SUPERVISOR_ENABLED",
  "ACA_CODER_SUPERVISOR_INTERVAL_SECONDS",
  "ACA_CODER_SUPERVISOR_BATCH_SIZE",
  "ACA_CODER_CANCEL_ON_SOURCE_TERMINAL",
  "ACA_ENABLE_SWARM",
  "ACA_SHARED_MODEL",
  "ACA_MAX_WORKERS",
  "ACA_MANAGER_PROVIDER",
  "ACA_MANAGER_MODEL",
  "ACA_WORKER_PROVIDER",
  "ACA_WORKER_MODEL",
  "ACA_REVIEWER_PROVIDER",
  "ACA_REVIEWER_MODEL",
  "ACA_TESTER_PROVIDER",
  "ACA_TESTER_MODEL",
  "ACA_OUTPUT_ROOT",
  "ACA_GITHUB_MCP_ENABLED",
  "ACA_GITHUB_MCP_URL",
  "ACA_GITHUB_MCP_TOOLSETS",
  "ACA_GITHUB_MCP_SCOPE",
  "ACA_GITHUB_REMOTE_SYNC",
  "ACA_LINEAR_MCP_ENABLED",
  "ACA_LINEAR_MCP_SERVER",
  "ACA_LINEAR_MCP_SCOPE",
  "ACA_LINEAR_REMOTE_SYNC",
  "ACA_LINEAR_CLAIM_LABEL",
  "ACA_LINEAR_DONE_LABEL",
  "ACA_LINEAR_BLOCKED_LABEL",
  "ACA_LINEAR_CLAIM_STATUS",
  "ACA_LINEAR_REVIEW_STATUS",
  "ACA_LINEAR_DONE_STATUS",
  "ACA_LINEAR_BLOCKED_STATUS",
  "GITHUB_PERSONAL_ACCESS_TOKEN_FILE",
  "GITHUB_TOKEN_FILE",
  "ACA_KB_MCP_ENABLED",
]);

const DEFAULT_CONTROL_PANEL_CONFIG = {
  version: 1,
  control_panel: {
    mode: "auto",
    aca_compact_nav: true,
  },
  agent: {
    name: "ACA",
    dry_run: false,
  },
  tandem: {
    base_url: "http://127.0.0.1:39733",
    token_env: "TANDEM_API_TOKEN",
    token_file: "tandem-data/tandem_api_token",
    required_version: "",
    startup_mode: "reuse_or_start",
    update_policy: "notify",
    engine_command: "scripts/tandem-engine-serve.sh",
  },
  task_source: {
    type: "kanban_board",
    owner: "",
    repo: "",
    team: "",
    project: "",
    statuses: "",
    labels: "",
    query: "",
    item: "",
    url: "",
    path: "config/board.yaml",
    prompt: "",
    source_name: "",
    card_id: "",
    payload: {},
  },
  repository: {
    path: "",
    slug: "",
    clone_url: "",
    default_branch: "main",
    worktree_root: "",
    remote_name: "origin",
  },
  provider: {
    id: "openai",
    model: "gpt-4.1-mini",
    base_url: "",
    fallback_provider: "",
    fallback_model: "",
  },
  execution: {
    backend: "auto",
    coder_wait_timeout_seconds: 3600,
    coder_poll_interval_seconds: 15,
    coder_supervisor_enabled: true,
    coder_supervisor_interval_seconds: 30,
    coder_supervisor_batch_size: 100,
    coder_cancel_on_source_terminal: true,
  },
  swarm: {
    enabled: false,
    shared_model: false,
    max_workers: 3,
    max_retries: 1,
    manager: { provider: "", model: "" },
    worker: { provider: "", model: "" },
    reviewer: { provider: "", model: "" },
    tester: { provider: "", model: "" },
  },
  output: {
    root: "runs",
  },
  github_mcp: {
    enabled: true,
    url: "https://api.githubcopilot.com/mcp/",
    toolsets: "default,projects",
    scope: "intake_finalize",
    remote_sync: "status_comment",
  },
  linear_mcp: {
    enabled: false,
    server: "linear",
    scope: "intake_finalize",
    remote_sync: "rich",
    claim_label: "aca-running",
    done_label: "aca-done",
    blocked_label: "aca-blocked",
    claim_status: "In Progress",
    review_status: "In Review",
    done_status: "Done",
    blocked_status: "Blocked",
  },
  hosted: {
    managed: false,
    provider: "",
    deployment_id: "",
    deployment_slug: "",
    hostname: "",
    public_url: "",
    control_plane_url: "",
  },
  mcp_servers: {},
};

function parseDotEnv(content) {
  const out = {};
  for (const raw of String(content || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    const key = line.slice(0, idx).trim();
    let value = line.slice(idx + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

function renderDotEnv(entries) {
  return `${entries.map(([key, value]) => `${key}=${value}`).join("\n")}\n`;
}

function firstNonEmpty(...values) {
  let defaultValue = "";
  if (values.length && typeof values[values.length - 1] === "object" && values[values.length - 1] !== null && Object.prototype.hasOwnProperty.call(values[values.length - 1], "default")) {
    defaultValue = String(values.pop().default ?? "");
  }
  for (const value of values) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return defaultValue;
}

function boolValue(value, defaultValue = false) {
  if (value === null || value === undefined) return defaultValue;
  if (typeof value === "boolean") return value;
  const text = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(text)) return true;
  if (["0", "false", "no", "n", "off"].includes(text)) return false;
  return defaultValue;
}

function intValue(value, defaultValue) {
  if (value === null || value === undefined) return defaultValue;
  const parsed = Number.parseInt(String(value).trim(), 10);
  return Number.isFinite(parsed) ? parsed : defaultValue;
}

function parseScalar(raw) {
  const value = String(raw ?? "").trim();
  if (!value) return "";
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  const lower = value.toLowerCase();
  if (lower === "true") return true;
  if (lower === "false") return false;
  if (lower === "null" || lower === "~") return null;
  if (/^-?\d+$/.test(value)) return Number.parseInt(value, 10);
  if (/^-?\d+\.\d+$/.test(value)) return Number.parseFloat(value);
  return value;
}

function loadSimpleYaml(pathname) {
  if (!existsSync(pathname)) return {};
  const lines = readFileSync(pathname, "utf8").split(/\r?\n/);
  const root = {};
  const stack = [{ indent: -1, value: root }];
  for (const rawLine of lines) {
    if (!rawLine.trim() || rawLine.trim().startsWith("#")) continue;
    const indent = rawLine.match(/^ */)?.[0].length ?? 0;
    const line = rawLine.trim();
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const container = stack[stack.length - 1].value;
    const sep = line.indexOf(":");
    if (sep < 0) continue;
    const key = line.slice(0, sep).trim();
    const rest = line.slice(sep + 1).trim();
    if (!rest) {
      const next = {};
      container[key] = next;
      stack.push({ indent, value: next });
      continue;
    }
    container[key] = parseScalar(rest);
  }
  return root;
}

function deepMerge(base, overlay) {
  if (Array.isArray(base) && Array.isArray(overlay)) {
    return overlay.slice();
  }
  if (
    base &&
    overlay &&
    typeof base === "object" &&
    typeof overlay === "object" &&
    !Array.isArray(base) &&
    !Array.isArray(overlay)
  ) {
    const out = { ...base };
    for (const [key, value] of Object.entries(overlay)) {
      out[key] = key in out ? deepMerge(out[key], value) : value;
    }
    return out;
  }
  return overlay === undefined ? base : overlay;
}

function normalizeControlPanelConfig(raw = {}) {
  const input = raw && typeof raw === "object" ? raw : {};
  return deepMerge(DEFAULT_CONTROL_PANEL_CONFIG, input);
}

function readJsonFile(pathname) {
  if (!existsSync(pathname)) return {};
  try {
    return JSON.parse(readFileSync(pathname, "utf8"));
  } catch {
    return {};
  }
}

function ensureTokenFile(pathname, preferred = "") {
  mkdirSync(dirname(pathname), { recursive: true });
  let current = "";
  if (existsSync(pathname)) {
    try {
      current = readFileSync(pathname, "utf8").trim();
    } catch {
      current = "";
    }
  }
  let token = firstNonEmpty(preferred, current);
  if (!token) {
    token = randomBytes(32).toString("base64url");
  }
  writeFileSync(pathname, `${token}\n`, "utf8");
  try {
    chmodSync(pathname, 0o600);
  } catch {}
  return token;
}

function buildControlPanelConfig(existing, example) {
  const base = loadSimpleYaml(AGENT_YAML);
  const rawExistingConfig = readJsonFile(CONTROL_PANEL_CONFIG_FILE);
  const existingConfig = normalizeControlPanelConfig(rawExistingConfig);
  const config = deepMerge(base, existingConfig);
  const boardExists = existsSync(BOARD_YAML);

  const taskSourceType = firstNonEmpty(
    existing.ACA_TASK_SOURCE_TYPE,
    existingConfig.task_source?.type,
    base.task_source?.type,
    { default: boardExists ? "kanban_board" : "manual" }
  );
  const taskSourcePath = firstNonEmpty(
    existing.ACA_TASK_SOURCE_PATH,
    existingConfig.task_source?.path,
    base.task_source?.path,
    { default: "config/board.yaml" }
  );
  const repoPath = firstNonEmpty(
    existing.ACA_REPO_PATH,
    existingConfig.repository?.path,
    { default: "" }
  );
  const providerId = firstNonEmpty(
    existing.ACA_PROVIDER,
    existingConfig.provider?.id,
    base.provider?.id,
    { default: "openai" }
  );
  const providerModel = firstNonEmpty(
    existing.ACA_MODEL,
    existingConfig.provider?.model,
    base.provider?.model,
    { default: "gpt-4.1-mini" }
  );
  const githubEnabledRaw = firstNonEmpty(
    existing.ACA_GITHUB_MCP_ENABLED,
    rawExistingConfig.mcp_servers?.github?.enabled,
    rawExistingConfig.github_mcp?.enabled
  );
  const linearEnabledRaw = firstNonEmpty(
    existing.ACA_LINEAR_MCP_ENABLED,
    rawExistingConfig.mcp_servers?.linear?.enabled,
    rawExistingConfig.linear_mcp?.enabled
  );

  return normalizeControlPanelConfig(
    deepMerge(config, {
      control_panel: {
        mode: firstNonEmpty(
          existing.TANDEM_CONTROL_PANEL_MODE,
          existingConfig.control_panel?.mode,
          { default: "auto" }
        ),
        aca_compact_nav: boolValue(
          existingConfig.control_panel?.aca_compact_nav,
          true
        ),
      },
      agent: {
        name: firstNonEmpty(
          existing.AGENT_NAME,
          existingConfig.agent?.name,
          base.agent?.name,
          { default: "ACA" }
        ),
        dry_run: boolValue(existing.ACA_DRY_RUN, false),
      },
      tandem: {
        base_url: firstNonEmpty(
          existing.TANDEM_BASE_URL,
          existingConfig.tandem?.base_url,
          base.tandem?.base_url,
          { default: "http://127.0.0.1:39733" }
        ),
        token_env: firstNonEmpty(
          existing.TANDEM_TOKEN_ENV,
          existingConfig.tandem?.token_env,
          base.tandem?.token_env,
          { default: "TANDEM_API_TOKEN" }
        ),
        token_file: firstNonEmpty(
          existing.TANDEM_API_TOKEN_FILE,
          existingConfig.tandem?.token_file,
          base.tandem?.token_file,
          { default: "tandem-data/tandem_api_token" }
        ),
        required_version: firstNonEmpty(
          existing.TANDEM_REQUIRED_VERSION,
          existingConfig.tandem?.required_version,
          { default: "" }
        ),
        startup_mode: firstNonEmpty(
          existing.TANDEM_STARTUP_MODE,
          existingConfig.tandem?.startup_mode,
          { default: "reuse_or_start" }
        ),
        update_policy: firstNonEmpty(
          existing.TANDEM_UPDATE_POLICY,
          existingConfig.tandem?.update_policy,
          { default: "notify" }
        ),
        engine_command: firstNonEmpty(
          existing.TANDEM_ENGINE_COMMAND,
          existingConfig.tandem?.engine_command,
          base.tandem?.engine_command,
          { default: "scripts/tandem-engine-serve.sh" }
        ),
      },
      task_source: {
        type: taskSourceType,
        owner: firstNonEmpty(existing.ACA_TASK_SOURCE_OWNER, existingConfig.task_source?.owner),
        repo: firstNonEmpty(existing.ACA_TASK_SOURCE_REPO, existingConfig.task_source?.repo),
        team: firstNonEmpty(existing.ACA_TASK_SOURCE_TEAM, existingConfig.task_source?.team),
        project: firstNonEmpty(existing.ACA_TASK_SOURCE_PROJECT, existingConfig.task_source?.project),
        statuses: firstNonEmpty(existing.ACA_TASK_SOURCE_STATUSES, existingConfig.task_source?.statuses),
        labels: firstNonEmpty(existing.ACA_TASK_SOURCE_LABELS, existingConfig.task_source?.labels),
        query: firstNonEmpty(existing.ACA_TASK_SOURCE_QUERY, existingConfig.task_source?.query),
        item: firstNonEmpty(existing.ACA_TASK_SOURCE_ITEM, existingConfig.task_source?.item),
        url: firstNonEmpty(existing.ACA_TASK_SOURCE_URL, existingConfig.task_source?.url),
        path: taskSourcePath,
        prompt: firstNonEmpty(
          existing.ACA_TASK_SOURCE_PROMPT,
          existingConfig.task_source?.prompt,
          { default: "Describe the task in the control panel." }
        ),
        source_name: firstNonEmpty(
          existing.ACA_TASK_SOURCE_SOURCE_NAME,
          existingConfig.task_source?.source_name
        ),
        card_id: firstNonEmpty(existing.ACA_TASK_SOURCE_CARD_ID, existingConfig.task_source?.card_id),
        payload: existingConfig.task_source?.payload || {},
      },
      repository: {
        path: repoPath,
        slug: firstNonEmpty(existing.ACA_REPO_SLUG),
        clone_url: firstNonEmpty(existing.ACA_REPO_URL),
        default_branch: firstNonEmpty(
          existing.ACA_DEFAULT_BRANCH,
          existingConfig.repository?.default_branch,
          base.repository?.default_branch,
          { default: "main" }
        ),
        worktree_root: firstNonEmpty(existing.ACA_WORKTREE_ROOT),
        remote_name: firstNonEmpty(
          existing.ACA_REMOTE_NAME,
          existingConfig.repository?.remote_name,
          { default: "origin" }
        ),
      },
      provider: {
        id: providerId,
        model: providerModel,
        base_url: firstNonEmpty(existing.ACA_PROVIDER_BASE_URL),
        fallback_provider: firstNonEmpty(existing.ACA_FALLBACK_PROVIDER),
        fallback_model: firstNonEmpty(existing.ACA_FALLBACK_MODEL),
      },
      execution: {
        backend: firstNonEmpty(existing.ACA_EXECUTION_BACKEND, { default: "auto" }),
        coder_wait_timeout_seconds: intValue(firstNonEmpty(existing.ACA_CODER_WAIT_TIMEOUT_SECONDS, existingConfig.execution?.coder_wait_timeout_seconds, { default: "3600" }), 3600),
        coder_poll_interval_seconds: intValue(firstNonEmpty(existing.ACA_CODER_POLL_INTERVAL_SECONDS, existingConfig.execution?.coder_poll_interval_seconds, { default: "15" }), 15),
        coder_supervisor_enabled: boolValue(firstNonEmpty(existing.ACA_CODER_SUPERVISOR_ENABLED, existingConfig.execution?.coder_supervisor_enabled, { default: "true" }), true),
        coder_supervisor_interval_seconds: intValue(firstNonEmpty(existing.ACA_CODER_SUPERVISOR_INTERVAL_SECONDS, existingConfig.execution?.coder_supervisor_interval_seconds, { default: "30" }), 30),
        coder_supervisor_batch_size: intValue(firstNonEmpty(existing.ACA_CODER_SUPERVISOR_BATCH_SIZE, existingConfig.execution?.coder_supervisor_batch_size, { default: "100" }), 100),
        coder_cancel_on_source_terminal: boolValue(firstNonEmpty(existing.ACA_CODER_CANCEL_ON_SOURCE_TERMINAL, existingConfig.execution?.coder_cancel_on_source_terminal, { default: "true" }), true),
      },
      swarm: {
        enabled: boolValue(existing.ACA_ENABLE_SWARM, false),
        shared_model: boolValue(existing.ACA_SHARED_MODEL, false),
        max_workers: intValue(firstNonEmpty(existing.ACA_MAX_WORKERS, { default: "3" }), 3),
        max_retries: intValue(existingConfig.swarm?.max_retries, 1),
        manager: {
          provider: firstNonEmpty(existing.ACA_MANAGER_PROVIDER),
          model: firstNonEmpty(existing.ACA_MANAGER_MODEL),
        },
        worker: {
          provider: firstNonEmpty(existing.ACA_WORKER_PROVIDER),
          model: firstNonEmpty(existing.ACA_WORKER_MODEL),
        },
        reviewer: {
          provider: firstNonEmpty(existing.ACA_REVIEWER_PROVIDER),
          model: firstNonEmpty(existing.ACA_REVIEWER_MODEL),
        },
        tester: {
          provider: firstNonEmpty(existing.ACA_TESTER_PROVIDER),
          model: firstNonEmpty(existing.ACA_TESTER_MODEL),
        },
      },
      output: {
        root: firstNonEmpty(
          existing.ACA_OUTPUT_ROOT,
          existingConfig.output?.root,
          base.output?.root,
          { default: "runs" }
        ),
      },
      mcp_servers: {
        ...(rawExistingConfig.mcp_servers && typeof rawExistingConfig.mcp_servers === "object" ? rawExistingConfig.mcp_servers : {}),
        github: {
          ...(githubEnabledRaw ? { enabled: boolValue(githubEnabledRaw, false) } : {}),
          transport: firstNonEmpty(
            existing.ACA_GITHUB_MCP_URL,
            rawExistingConfig.mcp_servers?.github?.transport,
            rawExistingConfig.github_mcp?.url,
            base.github_mcp?.url,
            { default: "https://api.githubcopilot.com/mcp/" }
          ),
          headers: firstNonEmpty(
            existing.ACA_GITHUB_MCP_TOOLSETS,
            rawExistingConfig.mcp_servers?.github?.headers?.["X-MCP-Toolsets"],
            rawExistingConfig.github_mcp?.toolsets,
            base.github_mcp?.toolsets
          )
            ? {
                "X-MCP-Toolsets": firstNonEmpty(
                  existing.ACA_GITHUB_MCP_TOOLSETS,
                  rawExistingConfig.mcp_servers?.github?.headers?.["X-MCP-Toolsets"],
                  rawExistingConfig.github_mcp?.toolsets,
                  base.github_mcp?.toolsets,
                  { default: "default,projects" }
                ),
              }
            : {},
          auth: {
            token_envs: ["GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"],
            token_file_envs: ["GITHUB_TOKEN_FILE", "GITHUB_PERSONAL_ACCESS_TOKEN_FILE"],
          },
          auto_connect: true,
          auto_enable_with_credentials: true,
          scope: firstNonEmpty(
            existing.ACA_GITHUB_MCP_SCOPE,
            rawExistingConfig.mcp_servers?.github?.scope,
            rawExistingConfig.github_mcp?.scope,
            base.github_mcp?.scope,
            { default: "intake_finalize" }
          ),
          remote_sync: firstNonEmpty(
            existing.ACA_GITHUB_REMOTE_SYNC,
            rawExistingConfig.mcp_servers?.github?.remote_sync,
            rawExistingConfig.github_mcp?.remote_sync,
            base.github_mcp?.remote_sync,
            { default: "status_comment" }
          ),
        },
        kb: {
          enabled: boolValue(
            firstNonEmpty(
              existing.ACA_KB_MCP_ENABLED,
              rawExistingConfig.mcp_servers?.kb?.enabled,
              { default: "true" }
            ),
            true
          ),
          transport: firstNonEmpty(
            existing.KB_PUBLIC_BASE_URL,
            existing.TANDEM_KB_MCP_URL,
            rawExistingConfig.mcp_servers?.kb?.transport,
            { default: "http://tandem-kb-mcp:39736/mcp" }
          ),
          auto_connect: true,
        },
        linear: {
          ...(linearEnabledRaw ? { enabled: boolValue(linearEnabledRaw, false) } : {}),
          name: firstNonEmpty(
            existing.ACA_LINEAR_MCP_SERVER,
            rawExistingConfig.mcp_servers?.linear?.name,
            rawExistingConfig.linear_mcp?.server,
            { default: "linear" }
          ),
          transport: firstNonEmpty(
            existing.ACA_LINEAR_MCP_URL,
            rawExistingConfig.mcp_servers?.linear?.transport,
            { default: "https://mcp.linear.app/mcp" }
          ),
          auth_kind: firstNonEmpty(
            rawExistingConfig.mcp_servers?.linear?.auth_kind,
            { default: "oauth" }
          ),
          auto_connect: true,
          scope: firstNonEmpty(
            existing.ACA_LINEAR_MCP_SCOPE,
            rawExistingConfig.mcp_servers?.linear?.scope,
            rawExistingConfig.linear_mcp?.scope,
            { default: "intake_finalize" }
          ),
          remote_sync: firstNonEmpty(
            existing.ACA_LINEAR_REMOTE_SYNC,
            rawExistingConfig.mcp_servers?.linear?.remote_sync,
            rawExistingConfig.linear_mcp?.remote_sync,
            { default: "rich" }
          ),
        },
      },
      linear_mcp: {
        ...(linearEnabledRaw ? { enabled: boolValue(linearEnabledRaw, false) } : {}),
        server: firstNonEmpty(
          existing.ACA_LINEAR_MCP_SERVER,
          rawExistingConfig.linear_mcp?.server,
          rawExistingConfig.mcp_servers?.linear?.name,
          { default: "linear" }
        ),
        scope: firstNonEmpty(
          existing.ACA_LINEAR_MCP_SCOPE,
          rawExistingConfig.linear_mcp?.scope,
          rawExistingConfig.mcp_servers?.linear?.scope,
          { default: "intake_finalize" }
        ),
        remote_sync: firstNonEmpty(
          existing.ACA_LINEAR_REMOTE_SYNC,
          rawExistingConfig.linear_mcp?.remote_sync,
          rawExistingConfig.mcp_servers?.linear?.remote_sync,
          { default: "rich" }
        ),
        claim_label: firstNonEmpty(
          existing.ACA_LINEAR_CLAIM_LABEL,
          rawExistingConfig.linear_mcp?.claim_label,
          rawExistingConfig.mcp_servers?.linear?.claim_label,
          { default: "aca-running" }
        ),
        done_label: firstNonEmpty(
          existing.ACA_LINEAR_DONE_LABEL,
          rawExistingConfig.linear_mcp?.done_label,
          rawExistingConfig.mcp_servers?.linear?.done_label,
          { default: "aca-done" }
        ),
        blocked_label: firstNonEmpty(
          existing.ACA_LINEAR_BLOCKED_LABEL,
          rawExistingConfig.linear_mcp?.blocked_label,
          rawExistingConfig.mcp_servers?.linear?.blocked_label,
          { default: "aca-blocked" }
        ),
        claim_status: firstNonEmpty(
          existing.ACA_LINEAR_CLAIM_STATUS,
          rawExistingConfig.linear_mcp?.claim_status,
          rawExistingConfig.mcp_servers?.linear?.claim_status,
          { default: "In Progress" }
        ),
        review_status: firstNonEmpty(
          existing.ACA_LINEAR_REVIEW_STATUS,
          rawExistingConfig.linear_mcp?.review_status,
          rawExistingConfig.mcp_servers?.linear?.review_status,
          { default: "In Review" }
        ),
        done_status: firstNonEmpty(
          existing.ACA_LINEAR_DONE_STATUS,
          rawExistingConfig.linear_mcp?.done_status,
          rawExistingConfig.mcp_servers?.linear?.done_status,
          { default: "Done" }
        ),
        blocked_status: firstNonEmpty(
          existing.ACA_LINEAR_BLOCKED_STATUS,
          rawExistingConfig.linear_mcp?.blocked_status,
          rawExistingConfig.mcp_servers?.linear?.blocked_status,
          { default: "Blocked" }
        ),
      },
      github_mcp: {
        ...(githubEnabledRaw ? { enabled: boolValue(githubEnabledRaw, false) } : {}),
        url: firstNonEmpty(
          existing.ACA_GITHUB_MCP_URL,
          rawExistingConfig.github_mcp?.url,
          rawExistingConfig.mcp_servers?.github?.transport,
          base.github_mcp?.url,
          { default: "https://api.githubcopilot.com/mcp/" }
        ),
        toolsets: firstNonEmpty(
          existing.ACA_GITHUB_MCP_TOOLSETS,
          rawExistingConfig.github_mcp?.toolsets,
          rawExistingConfig.mcp_servers?.github?.headers?.["X-MCP-Toolsets"],
          base.github_mcp?.toolsets,
          { default: "default,projects" }
        ),
        scope: firstNonEmpty(
          existing.ACA_GITHUB_MCP_SCOPE,
          rawExistingConfig.github_mcp?.scope,
          rawExistingConfig.mcp_servers?.github?.scope,
          base.github_mcp?.scope,
          { default: "intake_finalize" }
        ),
        remote_sync: firstNonEmpty(
          existing.ACA_GITHUB_REMOTE_SYNC,
          rawExistingConfig.github_mcp?.remote_sync,
          rawExistingConfig.mcp_servers?.github?.remote_sync,
          base.github_mcp?.remote_sync,
          { default: "status_comment" }
        ),
      },
      hosted: {
        managed: boolValue(
          firstNonEmpty(
            existing.TANDEM_HOSTED_MANAGED,
            existing.HOSTED_MANAGED,
            rawExistingConfig.hosted?.managed,
            base.hosted?.managed
          ),
          false
        ),
        provider: firstNonEmpty(existing.HOSTED_PROVIDER, rawExistingConfig.hosted?.provider),
        deployment_id: firstNonEmpty(
          existing.HOSTED_DEPLOYMENT_ID,
          rawExistingConfig.hosted?.deployment_id
        ),
        deployment_slug: firstNonEmpty(
          existing.HOSTED_DEPLOYMENT_SLUG,
          rawExistingConfig.hosted?.deployment_slug
        ),
        hostname: firstNonEmpty(existing.HOSTED_HOSTNAME, rawExistingConfig.hosted?.hostname),
        public_url: firstNonEmpty(
          existing.TANDEM_CONTROL_PANEL_PUBLIC_URL,
          existing.HOSTED_CONTROL_PANEL_PUBLIC_URL,
          rawExistingConfig.hosted?.public_url
        ),
        control_plane_url: firstNonEmpty(
          existing.HOSTED_CONTROL_PLANE_URL,
          rawExistingConfig.hosted?.control_plane_url,
          existing.TANDEM_CONTROL_PANEL_PUBLIC_URL,
          existing.HOSTED_CONTROL_PANEL_PUBLIC_URL
        ),
      },
    })
  );
}

function main() {
  if (!existsSync(ENV_EXAMPLE)) {
    console.error(`Missing template: ${ENV_EXAMPLE}`);
    process.exitCode = 1;
    return;
  }

  const existingEnv = parseDotEnv(existsSync(ENV_FILE) ? readFileSync(ENV_FILE, "utf8") : "");
  const exampleEnv = parseDotEnv(readFileSync(ENV_EXAMPLE, "utf8"));
  const config = buildControlPanelConfig(existingEnv, exampleEnv);
  mkdirSync(dirname(CONTROL_PANEL_CONFIG_FILE), { recursive: true });
  writeFileSync(CONTROL_PANEL_CONFIG_FILE, `${JSON.stringify(config, null, 2)}\n`, "utf8");

  const tandemToken = ensureTokenFile(
    TANDEM_TOKEN_FILE,
    firstNonEmpty(
      existingEnv.TANDEM_CONTROL_PANEL_ENGINE_TOKEN,
      existingEnv.TANDEM_API_TOKEN,
      exampleEnv.TANDEM_CONTROL_PANEL_ENGINE_TOKEN,
      exampleEnv.TANDEM_API_TOKEN
    )
  );
  const acaToken = ensureTokenFile(
    ACA_TOKEN_FILE,
    firstNonEmpty(existingEnv.ACA_API_TOKEN, exampleEnv.ACA_API_TOKEN)
  );

  const preserved = Object.entries(existingEnv).filter(([key, value]) => !MOVED_ENV_KEYS.has(key) && value !== "");
  const resolvedEnginePort = firstNonEmpty(
    existingEnv.TANDEM_ENGINE_PORT,
    exampleEnv.TANDEM_ENGINE_PORT,
    { default: "39733" }
  );
  const resolvedEngineHost = firstNonEmpty(
    existingEnv.TANDEM_ENGINE_HOST,
    exampleEnv.TANDEM_ENGINE_HOST,
    { default: "0.0.0.0" }
  );
  const normalizedEngineHost =
    ["127.0.0.1", "localhost", "::1"].includes(String(resolvedEngineHost).trim())
      ? "0.0.0.0"
      : resolvedEngineHost;
  const bootstrap = new Map([
    ["COMPOSE_PROJECT_NAME", firstNonEmpty(existingEnv.COMPOSE_PROJECT_NAME, exampleEnv.COMPOSE_PROJECT_NAME, { default: "tandem-aca" })],
    ["TANDEM_CONTROL_PANEL_PORT", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_PORT, exampleEnv.TANDEM_CONTROL_PANEL_PORT, { default: "39734" })],
    ["TANDEM_CONTROL_PANEL_HOST", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_HOST, exampleEnv.TANDEM_CONTROL_PANEL_HOST, { default: "127.0.0.1" })],
    ["TANDEM_CONTROL_PANEL_PUBLIC_URL", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_PUBLIC_URL, exampleEnv.TANDEM_CONTROL_PANEL_PUBLIC_URL)],
    ["TANDEM_CONTROL_PANEL_STATE_DIR", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_STATE_DIR, exampleEnv.TANDEM_CONTROL_PANEL_STATE_DIR, { default: "./tandem-data/control-panel" })],
    ["TANDEM_CONTROL_PANEL_CONFIG_FILE", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_CONFIG_FILE, exampleEnv.TANDEM_CONTROL_PANEL_CONFIG_FILE, { default: "./tandem-data/control-panel-config.json" })],
    ["TANDEM_CONTROL_PANEL_MODE", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_MODE, exampleEnv.TANDEM_CONTROL_PANEL_MODE, { default: "auto" })],
    ["TANDEM_CONTROL_PANEL_AUTO_START_ENGINE", firstNonEmpty(existingEnv.TANDEM_CONTROL_PANEL_AUTO_START_ENGINE, exampleEnv.TANDEM_CONTROL_PANEL_AUTO_START_ENGINE, { default: "1" })],
    ["TANDEM_ENGINE_URL", firstNonEmpty(existingEnv.TANDEM_ENGINE_URL, exampleEnv.TANDEM_ENGINE_URL, { default: `http://127.0.0.1:${resolvedEnginePort}` })],
    ["TANDEM_ENGINE_HOST", normalizedEngineHost],
    ["TANDEM_ENGINE_PORT", resolvedEnginePort],
    ["TANDEM_PORT", firstNonEmpty(existingEnv.TANDEM_PORT, exampleEnv.TANDEM_PORT, { default: "39733" })],
    ["TANDEM_API_TOKEN_FILE", firstNonEmpty(existingEnv.TANDEM_API_TOKEN_FILE, exampleEnv.TANDEM_API_TOKEN_FILE, { default: "./tandem-data/tandem_api_token" })],
    ["ACA_MODE", firstNonEmpty(existingEnv.ACA_MODE, exampleEnv.ACA_MODE, { default: "api" })],
    ["ACA_API_PORT", firstNonEmpty(existingEnv.ACA_API_PORT, exampleEnv.ACA_API_PORT, { default: "39735" })],
    ["ACA_API_TOKEN_FILE", firstNonEmpty(existingEnv.ACA_API_TOKEN_FILE, exampleEnv.ACA_API_TOKEN_FILE, { default: "./tandem-data/aca_api_token" })],
    ["ACA_HOST_UID", firstNonEmpty(existingEnv.ACA_HOST_UID, exampleEnv.ACA_HOST_UID)],
    ["ACA_HOST_GID", firstNonEmpty(existingEnv.ACA_HOST_GID, exampleEnv.ACA_HOST_GID)],
    ["ACA_WORKSPACE_HOST_DIR", firstNonEmpty(existingEnv.ACA_WORKSPACE_HOST_DIR, exampleEnv.ACA_WORKSPACE_HOST_DIR, { default: "./workspace" })],
    ["ACA_LOCAL_REPOS_DIR", firstNonEmpty(existingEnv.ACA_LOCAL_REPOS_DIR, exampleEnv.ACA_LOCAL_REPOS_DIR, { default: "./test-repos" })],
    ["ACA_GITHUB_MCP_ENABLED", firstNonEmpty(existingEnv.ACA_GITHUB_MCP_ENABLED, exampleEnv.ACA_GITHUB_MCP_ENABLED)],
    ["ACA_LINEAR_MCP_ENABLED", firstNonEmpty(existingEnv.ACA_LINEAR_MCP_ENABLED, exampleEnv.ACA_LINEAR_MCP_ENABLED)],
    ["ACA_LINEAR_MCP_SERVER", firstNonEmpty(existingEnv.ACA_LINEAR_MCP_SERVER, exampleEnv.ACA_LINEAR_MCP_SERVER)],
    ["ACA_LINEAR_MCP_URL", firstNonEmpty(existingEnv.ACA_LINEAR_MCP_URL, exampleEnv.ACA_LINEAR_MCP_URL)],
    ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE", firstNonEmpty(existingEnv.GITHUB_PERSONAL_ACCESS_TOKEN_FILE, exampleEnv.GITHUB_PERSONAL_ACCESS_TOKEN_FILE, { default: "./secrets/github_token" })],
    ["GITHUB_TOKEN_FILE", firstNonEmpty(existingEnv.GITHUB_TOKEN_FILE, exampleEnv.GITHUB_TOKEN_FILE, { default: "./secrets/github_token" })],
    ["ACA_KB_MCP_ENABLED", firstNonEmpty(existingEnv.ACA_KB_MCP_ENABLED, exampleEnv.ACA_KB_MCP_ENABLED, { default: "true" })],
    ["GITHUB_PERSONAL_ACCESS_TOKEN", firstNonEmpty(existingEnv.GITHUB_PERSONAL_ACCESS_TOKEN, exampleEnv.GITHUB_PERSONAL_ACCESS_TOKEN)],
    ["GITHUB_TOKEN", firstNonEmpty(existingEnv.GITHUB_TOKEN, exampleEnv.GITHUB_TOKEN)],
    ["OPENAI_API_KEY", firstNonEmpty(existingEnv.OPENAI_API_KEY, exampleEnv.OPENAI_API_KEY)],
    ["OPENROUTER_API_KEY", firstNonEmpty(existingEnv.OPENROUTER_API_KEY, exampleEnv.OPENROUTER_API_KEY)],
    ["ANTHROPIC_API_KEY", firstNonEmpty(existingEnv.ANTHROPIC_API_KEY, exampleEnv.ANTHROPIC_API_KEY)],
    ["GROQ_API_KEY", firstNonEmpty(existingEnv.GROQ_API_KEY, exampleEnv.GROQ_API_KEY)],
    ["MISTRAL_API_KEY", firstNonEmpty(existingEnv.MISTRAL_API_KEY, exampleEnv.MISTRAL_API_KEY)],
    ["TOGETHER_API_KEY", firstNonEmpty(existingEnv.TOGETHER_API_KEY, exampleEnv.TOGETHER_API_KEY)],
    ["COHERE_API_KEY", firstNonEmpty(existingEnv.COHERE_API_KEY, exampleEnv.COHERE_API_KEY)],
  ]);

  const finalEnv = new Map(preserved);
  for (const [key, value] of bootstrap.entries()) {
    if (value !== null && value !== undefined) {
      finalEnv.set(key, String(value));
    }
  }

  writeFileSync(ENV_FILE, renderDotEnv([...finalEnv.entries()]), "utf8");

  console.log("ACA setup bootstrap complete.");
  console.log(`  Env file:        ${ENV_FILE}`);
  console.log(`  Config file:     ${CONTROL_PANEL_CONFIG_FILE}`);
  console.log(`  Tandem token:    ${TANDEM_TOKEN_FILE}`);
  console.log(`  ACA token:       ${ACA_TOKEN_FILE}`);
  console.log(
    `  Tandem token value (use for control panel sign-in): ${tandemToken}`
  );
  console.log(
    `  Control panel:   http://${finalEnv.get("TANDEM_CONTROL_PANEL_HOST")}:${finalEnv.get("TANDEM_CONTROL_PANEL_PORT")}`
  );
}

main();
