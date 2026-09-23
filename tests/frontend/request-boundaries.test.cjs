'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/opsgraph/web/static/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('  function validWorkspaceKey('), source.indexOf('  function notice('));
const origin = 'http://127.0.0.1:8000';
const context = { URL, location: { origin } };
const guards = vm.runInNewContext(`${helpers}\n({apiPath, runId, validWorkspaceKey})`, context);

test('requests retain approved same-origin routes and encoded source identifiers', () => {
  for (const route of ['/api/runs', '/api/runs/inv-123/export', '/api/runs/inv-123/events?after=42', '/api/sources/source%20one/schema']) {
    assert.equal(guards.apiPath(route), origin + route);
  }
});
test('request boundary rejects other origins, normalized paths and invalid path segments', () => {
  for (const route of ['https://example.invalid/api/runs', '//example.invalid/api/runs', '/other', '/api/../settings', '/api/%2e%2e/settings', '/api/runs/a%2fb', '/api/runs/a%5cb', '/api/runs/a\nb', '/api/runs#fragment']) {
    assert.throws(() => guards.apiPath(route));
  }
});
test('generated and legacy run identifiers remain usable; non-identifiers are rejected', () => {
  for (const id of ['inv-123abc', 'legacy_run-1', '01234567-89ab-cdef-0123-456789abcdef']) assert.equal(guards.runId(id), id);
  for (const id of [null, {}, '', '.', '..', 'a/b', 'a\\b', 'id\n']) assert.throws(() => guards.runId(id));
});
test('opaque printable workspace keys are preserved without rewriting', () => {
  for (const value of ['fixture-key', 'a long passphrase with spaces', 'opaque!@#$%^&*()_+']) assert.equal(guards.validWorkspaceKey(value), true);
  for (const value of [null, {}, '', '   ', 'key\n', 'key\r', 'key\u0000']) assert.equal(guards.validWorkspaceKey(value), false);
});
test('invalid request paths fail before credentials or fetch are accessed', async () => {
  const helper = source.slice(source.indexOf('  async function api('), source.indexOf('  function showView('));
  const calls = [];
  const api = vm.runInNewContext(`(${helper.trim()})`, {
    apiPath: guards.apiPath,
    key: () => { calls.push('key'); return 'fixture-key'; },
    fetch: () => { calls.push('fetch'); },
  });
  await assert.rejects(api('https://example.invalid/api/runs'));
  assert.deepEqual(calls, []);
});
test('normal API calls prohibit redirects and retain workspace authentication', async () => {
  const helper = source.slice(source.indexOf('  async function api('), source.indexOf('  function showView('));
  let request;
  const api = vm.runInNewContext(`(${helper.trim()})`, {
    apiPath: guards.apiPath, key: () => 'fixture-key',
    fetch: async (url, options) => { request = {url, options}; return {ok: true, json: async () => ({ok: true})}; },
  });
  await api('/api/runs', {redirect: 'follow'});
  assert.equal(request.url, origin + '/api/runs');
  assert.equal(request.options.redirect, 'error');
  assert.equal(request.options.cache, 'no-store');
  assert.equal(request.options.headers['X-OpsGraph-Key'], 'fixture-key');
});
test('workspace-key validation prohibits redirects and caching', async () => {
  const helper = source.slice(source.indexOf('  async function connectWorkspace('), source.indexOf('  function clearWorkspace('));
  const nodes = new Map([
    ['#workspaceKey', {value: 'fixture-key'}],
    ['#saveCredential', {disabled: false}],
  ]);
  let request;
  const storage = new Map();
  const connectWorkspace = vm.runInNewContext(`(${helper.trim()})`, {
    $: selector => nodes.get(selector) || {value: '', disabled: false},
    state: {authEpoch: 0},
    guardAsyncFocus: () => () => {},
    notice: () => {},
    validWorkspaceKey: guards.validWorkspaceKey,
    apiPath: guards.apiPath,
    fetch: async (url, options) => {
      request = {url, options};
      return {ok: true, status: 200};
    },
    sessionStorage: {
      setItem: (key, value) => storage.set(key, value),
      getItem: key => storage.get(key),
    },
    loadWorkspace: async () => {},
    closeDrawer: () => {},
    openRun: async () => {},
    readiness: () => {},
  });
  await connectWorkspace({preventDefault() {}});
  assert.equal(request.url, origin + '/api/sources');
  assert.equal(request.options.redirect, 'error');
  assert.equal(request.options.cache, 'no-store');
  assert.equal(request.options.headers['X-OpsGraph-Key'], 'fixture-key');
  assert.equal(storage.get('opsgraph.workspaceKey'), 'fixture-key');
});
