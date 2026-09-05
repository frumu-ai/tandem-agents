// Execute the pinned panel source's real reader without launching its HTTP server.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import vm from 'node:vm';

const root = process.env.TANDEM_TEST_PANEL;
assert.ok(root, 'TANDEM_TEST_PANEL must point at the pinned panel package');
const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
assert.equal(pkg.version, '0.7.2');
const { summarizeControlPanelConfig } = await import(pathToFileURL(resolve(root, 'lib/setup/control-panel-config.js')));
const { hostedAuthEndpoint } = await import(pathToFileURL(resolve(root, 'lib/setup/hosted-auth-endpoint.js')));
const source = readFileSync(resolve(root, 'bin/setup.js'), 'utf8');
const start = source.indexOf('function getHostedPanelAuthConfig()');
const end = source.indexOf('function hostedPanelAuthAvailable()', start);
assert.ok(start >= 0 && end > start, 'released panel auth reader must be present');
const config = JSON.parse(readFileSync(0, 'utf8'));
const context = {
  readControlPanelConfig: () => config,
  getControlPanelConfigPath: () => '/synthetic/config.json',
  summarizeControlPanelConfig,
};
const auth = vm.runInNewContext(source.slice(start, end) + '\ngetHostedPanelAuthConfig()', context);
assert.equal(auth.managed, true);
assert.equal(auth.authMode, 'hosted');
assert.equal(auth.hostAgentTokenFile, '/run/tandem-panel-auth/host-agent-token');
assert.equal(auth.panelLoginUrl, 'https://login.example.com/hosted/panel/authorize');
for (const operation of ['exchange', 'refresh']) {
  const endpoint = hostedAuthEndpoint(auth, operation);
  assert.equal(endpoint.origin, 'https://identity.example.com');
  assert.ok(endpoint.pathname.endsWith(`/panel/${operation}`));
}
console.log('Pinned panel auth reader and endpoint validation passed.');
