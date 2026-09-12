'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function fixture() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', checked: false, hidden: false, disabled: false, textContent: '', classList: { toggle() {} }, setAttribute() {}, removeAttribute() {} });
    return nodes.get(id);
  };
  const state = { authenticated: true, providerBusy: false, providerDirty: true, providerTestToken: 0, authEpoch: 0, modelTested: true, sources: [], runs: [] };
  const context = vm.createContext({ $, state, stamp: value => value, notice: (id, value = '') => { $(id).textContent = value; }, readiness() {}, guardAsyncFocus: () => () => {}, loadProvider: async () => {}, api: async () => ({}) });
  const begin = source.indexOf('  function providerFormMode()');
  const end = source.indexOf('  async function loadSources()', begin);
  vm.runInContext(source.slice(begin, end), context);
  return { $, state, context };
}
const config = { provider: 'ollama', model: 'qwen3:8b', endpoint: 'http://127.0.0.1:11434/v1', schema_profile: 'ollama', reasoning_effort: 'none', timeout_seconds: 300, api_key_configured: true, deployment_egress_enabled: false, allow_external_egress: false };

test('saved provider configuration never fills a credential and respects deployment egress ceiling', () => {
  const f = fixture(); f.$('#modelApiKey').value = 'unsaved secret';
  f.context.fillProviderForm(config);
  assert.equal(f.$('#modelApiKey').value, '');
  assert.equal(f.$('#modelExternalEgress').disabled, true);
  assert.match(f.$('#modelKeyStatus').textContent, /same provider and endpoint/);
  assert.equal(f.state.providerDirty, false);
});

test('successful save sends a key only to backend configuration endpoint and invalidates old probe', async () => {
  const f = fixture(); f.context.fillProviderForm(config); f.$('#modelApiKey').value = 'fixture-secret';
  let request;
  f.context.api = async (path, options) => { request = { path, ...JSON.parse(options.body) }; assert.equal(f.$('#modelApiKey').value, ''); return config; };
  await f.context.saveProviderConfiguration({ preventDefault() {} });
  assert.equal(request.path, '/api/providers/configuration'); assert.equal(request.api_key, 'fixture-secret');
  assert.equal(request.clear_api_key, false); assert.equal(f.state.modelTested, false); assert.equal(f.state.providerBusy, false);
  assert.match(f.$('#providerSaveStatus').textContent, /^Saved/);
});

test('failed save preserves prior successful probe and removes entered secret', async () => {
  const f = fixture(); f.context.fillProviderForm(config); f.$('#modelApiKey').value = 'fixture-secret';
  f.context.api = async () => { const error = new Error('Finish the active investigation first.'); error.status = 409; throw error; };
  await f.context.saveProviderConfiguration({ preventDefault() {} });
  assert.equal(f.state.modelTested, true); assert.equal(f.$('#modelApiKey').value, '');
  assert.match(f.$('#providerSaveError').textContent, /active investigation/);
});

test('unsaved edits and active saves prevent probing a different configuration', async () => {
  const f = fixture(); let called = false; f.context.api = async () => { called = true; };
  await f.context.testProvider(); assert.equal(called, false);
  f.state.providerDirty = false; f.state.providerBusy = true;
  await f.context.testProvider(); assert.equal(called, false);
});

test('a late probe cannot mark a disconnected or replaced configuration complete', async () => {
  const f = fixture(); f.state.providerDirty = false;
  let resolve; f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.testProvider(); f.state.providerTestToken++; resolve({ ok: true }); await pending;
  assert.equal(f.state.modelTested, false);
});

test('onboarding hides redundant connect action and marks only actual ready sources complete', () => {
  const f = fixture(); f.state.modelTested = false;
  f.context.terminal = () => true; f.context.renderComposerScope = () => {};
  const code = source.slice(source.indexOf('  function readiness()'), source.indexOf('  function scopeMarkup('));
  vm.runInContext(code, f.context);
  f.state.sources = [{ kind: 'postgresql', status: 'configured' }]; f.context.readiness();
  assert.equal(f.$('#setupCredential').hidden, true);
  assert.match(f.$('#workspaceStepTitle').textContent, /complete/);
  assert.doesNotMatch(f.$('#sourceStepTitle').textContent, /complete/);
  f.state.sources[0].status = 'ready'; f.context.readiness();
  assert.match(f.$('#sourceStepTitle').textContent, /complete/);
  assert.doesNotMatch(f.$('#modelStepTitle').textContent, /complete/);
  f.state.authenticated = false; f.context.readiness(); assert.equal(f.$('#setupCredential').hidden, false);
});


test('ambiguous save transport failure invalidates the earlier probe', async () => {
  const f = fixture(); f.context.fillProviderForm(config); f.state.modelTested = true;
  f.context.api = async () => { throw new Error('Connection lost'); };
  await f.context.saveProviderConfiguration({ preventDefault() {} });
  assert.equal(f.state.modelTested, false);
});

test('loading a changed revision invalidates a previously successful probe', () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'old' }); f.state.modelTested = true;
  f.context.fillProviderForm({ ...config, revision: 'new' }); assert.equal(f.state.modelTested, false);
});

test('another tab changing configuration during a probe cannot receive completed status', async () => {
  const f = fixture(); f.state.providerDirty = false;
  f.context.api = async path => path.endsWith('/test') ? { ok: true, configuration_revision: 'old' } : { ...config, revision: 'new' };
  await f.context.testProvider(); assert.equal(f.state.modelTested, false);
  assert.match(f.$('#providerError').textContent, /changed during/);
});

test('status refresh failure after a successful probe does not leave green completion', async () => {
  const f = fixture(); f.state.providerDirty = false;
  f.context.api = async path => path.endsWith('/test') ? { ok: true, configuration_revision: 'same' } : { ...config, revision: 'same' };
  f.context.loadProvider = async () => { throw new Error('Cannot refresh status'); };
  await f.context.testProvider(); assert.equal(f.state.modelTested, false);
});

test('disconnect during configuration load or save discards late responses', async () => {
  for (const operation of ['loadProviderConfiguration', 'saveProviderConfiguration']) {
    const f = fixture(); f.context.fillProviderForm(config);
    let resolve; f.context.api = () => new Promise(done => { resolve = done; });
    const pending = f.context[operation]({ preventDefault() {} });
    f.state.authEpoch++; f.state.providerConfiguration = null;
    resolve({ ...config, model: 'different' }); await pending;
    assert.equal(f.state.providerConfiguration, null);
  }
});
