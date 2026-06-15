#!/usr/bin/env node
const { existsSync, readFileSync, writeFileSync } = require("fs");
const { resolve } = require("path");

function readJsonFile(pathname) {
  if (!existsSync(pathname)) return {};
  try {
    const parsed = JSON.parse(readFileSync(pathname, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function writeJsonFile(pathname, value) {
  writeFileSync(pathname, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function firstNonEmpty(...values) {
  for (const value of values) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) return text;
  }
  return "";
}

function boolValue(value, defaultValue = false) {
  if (value === null || value === undefined) return defaultValue;
  if (typeof value === "boolean") return value;
  const text = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(text)) return true;
  if (["0", "false", "no", "n", "off"].includes(text)) return false;
  return defaultValue;
}

function normalizeHeaders(headers) {
  if (!headers || typeof headers !== "object" || Array.isArray(headers)) return {};
  const out = {};
  for (const [key, value] of Object.entries(headers)) {
    const text = firstNonEmpty(value);
    if (text) {
      out[key] = text;
    }
  }
  return out;
}

function readSecretFromSources(spec) {
  const auth = spec && typeof spec === "object" ? spec.auth : null;
  if (!auth || typeof auth !== "object") return "";

  const envNames = [
    ...(Array.isArray(auth.token_envs) ? auth.token_envs : []),
    ...(Array.isArray(auth.envs) ? auth.envs : []),
  ];
  const fileEnvNames = [
    ...(Array.isArray(auth.token_file_envs) ? auth.token_file_envs : []),
    ...(Array.isArray(auth.file_envs) ? auth.file_envs : []),
  ];

  for (const name of envNames) {
    const token = firstNonEmpty(process.env[name]);
    if (token) return token;
  }

  for (const name of fileEnvNames) {
    const candidate = firstNonEmpty(process.env[name]);
    if (!candidate) continue;
    const filePath = resolve(candidate);
    if (!existsSync(filePath)) continue;
    try {
      const token = readFileSync(filePath, "utf8").trim();
      if (token) return token;
    } catch {
      continue;
    }
  }

  const directToken = firstNonEmpty(auth.token);
  if (directToken) return directToken;
  return "";
}

function normalizeDesiredServers(rawConfig) {
  const config = rawConfig && typeof rawConfig === "object" ? rawConfig : {};
  const servers = config.mcp_servers && typeof config.mcp_servers === "object" && !Array.isArray(config.mcp_servers)
    ? { ...config.mcp_servers }
    : {};

  if (!servers.github && config.github_mcp && typeof config.github_mcp === "object") {
    servers.github = {
      enabled: config.github_mcp.enabled,
      transport: config.github_mcp.url,
      headers: config.github_mcp.toolsets
        ? { "X-MCP-Toolsets": config.github_mcp.toolsets }
        : {},
      auth: {
        token_envs: ["GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"],
        token_file_envs: ["GITHUB_PERSONAL_ACCESS_TOKEN_FILE", "GITHUB_TOKEN_FILE"],
      },
      auto_enable_with_credentials: true,
      auto_connect: true,
    };
  }

  return servers;
}

function applySpec(name, spec, existing) {
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) {
    return existing && typeof existing === "object" ? existing : {};
  }

  const next = {
    ...(existing && typeof existing === "object" ? existing : {}),
  };
  next.name = firstNonEmpty(spec.name, next.name, name);

  const hasExplicitEnabled = Object.prototype.hasOwnProperty.call(spec, "enabled");
  const enabledByConfig = hasExplicitEnabled ? boolValue(spec.enabled, false) : false;
  const secret = readSecretFromSources(spec);
  const shouldAutoEnable =
    !hasExplicitEnabled && boolValue(spec.auto_enable_with_credentials, true) && Boolean(secret);
  next.enabled = enabledByConfig || shouldAutoEnable;
  next.connected = false;

  const transport = firstNonEmpty(spec.transport, spec.url);
  if (transport) {
    next.transport = transport;
  }

  next.auto_connect = boolValue(
    spec.auto_connect,
    boolValue(existing && typeof existing === "object" ? existing.auto_connect : false)
  );
  next.auto_enable_with_credentials = boolValue(
    spec.auto_enable_with_credentials,
    boolValue(existing && typeof existing === "object" ? existing.auto_enable_with_credentials : false)
  );

  const scope = firstNonEmpty(spec.scope, existing && typeof existing === "object" ? existing.scope : "");
  if (scope) {
    next.scope = scope;
  }

  const remoteSync = firstNonEmpty(spec.remote_sync, existing && typeof existing === "object" ? existing.remote_sync : "");
  if (remoteSync) {
    next.remote_sync = remoteSync;
  }

  const headers = {
    ...normalizeHeaders(next.headers),
    ...normalizeHeaders(spec.headers),
  };

  const auth = spec.auth && typeof spec.auth === "object" ? spec.auth : {};
  const authKind = firstNonEmpty(spec.auth_kind, spec.authKind, next.auth_kind);
  if (authKind) {
    next.auth_kind = authKind;
  }

  const headerName = firstNonEmpty(auth.header_name, "Authorization");
  const headerPrefix = firstNonEmpty(auth.header_prefix, "Bearer");
  if (secret && next.enabled) {
    headers[headerName] = headerPrefix ? `${headerPrefix} ${secret}` : secret;
  }
  next.headers = headers;

  if (spec.oauth && typeof spec.oauth === "object" && !Array.isArray(spec.oauth)) {
    next.oauth = {
      ...(next.oauth && typeof next.oauth === "object" ? next.oauth : {}),
      ...spec.oauth,
    };
  }

  delete next.mcp_session_id;
  delete next.tool_cache;
  delete next.tools_fetched_at_ms;

  return next;
}

function main() {
  const sourcePath = firstNonEmpty(process.env.MCP_SOURCE_FILE, process.env.TANDEM_CONTROL_PANEL_CONFIG_FILE);
  const registryPath = firstNonEmpty(process.env.MCP_REGISTRY_FILE);
  if (!sourcePath || !registryPath) {
    process.exit(0);
  }

  const sourceConfig = readJsonFile(sourcePath);
  const desiredServers = normalizeDesiredServers(sourceConfig);
  const registry = readJsonFile(registryPath);
  const nextRegistry = { ...registry };

  for (const [name, spec] of Object.entries(desiredServers)) {
    const existing = registry[name];
    nextRegistry[name] = applySpec(name, spec, existing);
  }

  writeJsonFile(registryPath, nextRegistry);
  process.stdout.write(`${JSON.stringify(nextRegistry, null, 2)}\n`);
}

main();
