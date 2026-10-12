'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function fixture() {
  const nodes = new Map(), calls = [], items = new Map([
    ['opsgraph.workspaceKey', 'fixture-key'], ['opsgraph.selectedRun', 'saved-v1'],
  ]);
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: id === '#workspaceKey' ? 'fixture-key' : '', disabled: false, hidden: true });
    return nodes.get(id);
  };
  const state = { authEpoch: 0, authenticated: false, sources: [], savedConnection: null };
  const catalog = deferred();
  const context = vm.createContext({
    $, state, launchToken: null, notice() {},
    loadSources: async () => {}, loadSkills: async () => {},
    loadProviderConfiguration: async () => {}, loadProvider: async () => {},
    loadHistory: async () => {}, loadPolicy: async () => {}, loadHostingGuides: async () => {},
    workOutcomes: { connect: () => { calls.push('audit-load'); return catalog.promise; } },
    readiness: () => calls.push('ready'), sourceSetup: () => calls.push('source-setup'),
    guardAsyncFocus: () => () => {}, closeDrawer: () => calls.push('close'),
    openRun: async id => calls.push(`open:${id}`), validWorkspaceKey: () => true,
    apiPath: path => path, fetch: async () => ({ ok: true }),
    key: () => items.get('opsgraph.workspaceKey'),
    sessionStorage: { getItem: k => items.get(k), setItem: (k, v) => items.set(k, v) },
  });
  vm.runInContext(source.slice(source.indexOf('  async function loadWorkspace()'), source.indexOf('  function clearWorkspace()')), context);
  vm.runInContext(source.slice(source.indexOf('  async function startWorkspace()'), source.indexOf('  readiness(); void loadBootstrap();')), context);
  return { $, state, calls, context, catalog };
}
test('V1 connection closes and reopens saved work while audit catalog remains pending', async () => {
  const f = fixture(); let done = false;
  const connection = f.context.connectWorkspace({ preventDefault() {} }).then(() => { done = true; });
  try {
    await flush();
    assert.equal(done, true);
    assert.equal(f.$('#saveCredential').disabled, false);
    assert.equal(f.$('#workspaceKey').value, '');
    assert.equal(f.state.authenticated, true);
    assert.ok(f.calls.includes('close'));
    assert.ok(f.calls.includes('open:saved-v1'));
    assert.equal(f.calls.filter(c => c === 'audit-load').length, 1);
  } finally { f.catalog.resolve(); await connection; }
});
test('V1 reload restores the selected investigation without waiting for audit loading', async () => {
  const f = fixture(); let done = false;
  const startup = f.context.startWorkspace().then(() => { done = true; });
  try {
    await flush();
    assert.equal(done, true);
    assert.ok(f.calls.includes('ready'));
    assert.ok(f.calls.includes('open:saved-v1'));
  } finally { f.catalog.resolve(); await startup; }
});
test('a disconnect during workspace loading cannot start audits or restore private history', async () => {
  const f = fixture(), sources = deferred();
  f.context.loadSources = () => sources.promise;
  const startup = f.context.startWorkspace();
  f.state.authEpoch++;
  sources.resolve(); await startup;
  assert.deepEqual(f.calls, []);
});

test('a new navigation choice during reload cannot be overwritten by stored view or investigation restoration', async () => {
  const f = fixture(), loading = deferred();
  f.state.sourceWizard = { session: () => ({view:'work-outcomes',sourceId:null}) };
  f.context.loadWorkspace = () => loading.promise;
  f.context.showView = name => f.calls.push('view:'+name);
  const startup = f.context.startWorkspace();
  f.state.navigationEpoch = 1; f.context.showView('sources');
  loading.resolve(); await startup;
  assert.deepEqual(f.calls,['view:sources']);
});

test('connecting a workspace respects a navigation choice made during metadata loading', async () => {
  const f = fixture(), loading = deferred();
  f.context.loadWorkspace = () => loading.promise;
  const connecting = f.context.connectWorkspace({preventDefault(){}});
  await flush(); f.state.navigationEpoch = 1;
  loading.resolve(); await connecting;
  assert.equal(f.calls.some(call => call.startsWith('open:')),false);
});
