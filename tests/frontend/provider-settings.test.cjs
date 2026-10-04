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
  const providerPresets = {
    ollama: { endpoint: 'http://127.0.0.1:11434/v1', local: true },
    openai: { endpoint: 'https://api.openai.com/v1', fixed: true },
    anthropic: { endpoint: '', fixed: true },
    custom_openai: { endpoint: '' },
  };
  const sourceReady = item => item?.status === 'ready';
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({ $, state, setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id), esc: value => value, providerPresets, sourceReady, sourceReadinessPassed: item => sourceReady(item) && item.readiness?.status === 'ready', stamp: value => value, notice: (id, value = '') => { $(id).textContent = value; }, readiness() {}, guardAsyncFocus: () => () => {}, loadProvider: async () => {}, api: async () => ({}) });
  const begin = source.indexOf('  function clearProviderVerification()');
  const end = source.indexOf('  async function loadSources()', begin);
  vm.runInContext(source.slice(begin, end), context);
  vm.runInContext(source.slice(source.indexOf('  function isConversationQuestion('), source.indexOf('  function readiness()')), context);
  return { $, state, context, timers };
}
const verification = revision => ({ status: 'verified', configuration_revision: revision, checked_at: '2026-10-04T00:00:00Z', expires_at: '2026-10-04T00:15:00Z', valid_for_seconds: 899, detail: 'Recent model connection check passed.' });
const config = { provider: 'ollama', adapter: 'openai_compatible', model: 'qwen3:8b', endpoint: 'http://127.0.0.1:11434/v1', schema_profile: 'ollama', reasoning_effort: null, timeout_seconds: 300, max_output_tokens: 1024, api_key_configured: true, deployment_egress_enabled: false, allow_external_egress: false };

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
  f.context.api = async (path, options) => { if (path === '/api/providers/current') return {}; request = { path, ...JSON.parse(options.body) }; assert.equal(f.$('#modelApiKey').value, ''); return config; };
  await f.context.saveProviderConfiguration({ preventDefault() {} });
  assert.equal(request.path, '/api/providers/configuration'); assert.equal(request.api_key, 'fixture-secret');
  assert.equal(request.max_output_tokens, 1024);
  assert.equal(request.clear_api_key, false); assert.equal(f.state.modelTested, false); assert.equal(f.state.providerBusy, false);
  assert.match(f.$('#providerSaveStatus').textContent, /^Saved/);
});

test('hosted presets keep official endpoints while custom endpoints stay editable', () => {
  const f = fixture();
  f.$('#modelProvider').value = 'openai'; f.$('#modelEndpoint').value = 'https://api.openai.com/v1';
  f.context.providerFormMode();
  assert.equal(f.$('#modelEndpoint').readOnly, true);
  assert.match(f.$('#modelEndpointHelp').textContent, /official endpoint/);
  f.$('#modelProvider').value = 'custom_openai'; f.context.providerFormMode();
  assert.equal(f.$('#modelEndpoint').readOnly, false);
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
  const code = source.slice(source.indexOf('  function selectedInvestigationSource('), source.indexOf('  function scopeMarkup('));
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

test('readiness belongs to the selected source instead of any verified source', () => {
  const f = fixture();
  f.context.terminal = () => true; f.context.renderComposerScope = () => {};
  const code = source.slice(source.indexOf('  function selectedInvestigationSource('), source.indexOf('  function scopeMarkup('));
  vm.runInContext(code, f.context);
  f.state.providerDirty = false; f.state.modelTested = true;
  f.state.sources = [
    { id: 'verified', name: 'Verified', kind: 'postgresql', status: 'ready', readiness: { status: 'ready' } },
    { id: 'pending', name: 'Pending', kind: 'postgresql', status: 'ready', readiness: { status: 'pending' } },
  ];
  f.$('#investigationSource').value = 'pending'; f.context.readiness();
  assert.doesNotMatch(f.$('#readinessStepTitle').textContent, /complete/);
  assert.equal(f.$('#submitRun').disabled, true);
  assert.equal(f.$('#setupReadiness').textContent, 'Run readiness check');
  f.$('#investigationSource').value = 'verified'; f.context.readiness();
  assert.match(f.$('#readinessStepTitle').textContent, /complete/);
  assert.equal(f.$('#submitRun').disabled, false);
  f.state.sourceDirty = true; f.state.sourceEditingId = 'verified'; f.context.readiness();
  assert.equal(f.$('#submitRun').disabled, true);
  assert.match(f.$('#sourceFieldHelp').textContent, /unsaved edits/i);
});

test('readiness setup prioritizes the selected source, then an unchecked source', () => {
  const f = fixture();
  const code = source.slice(source.indexOf('  function selectedInvestigationSource('), source.indexOf('  function readiness()'));
  vm.runInContext(code, f.context);
  const verified = { id: 'verified', kind: 'postgresql', status: 'ready', readiness: { status: 'ready' } };
  const pending = { id: 'pending', kind: 'postgresql', status: 'ready', readiness: { status: 'pending' } };
  f.state.sources = [verified, pending];
  f.$('#investigationSource').value = 'verified';
  assert.equal(f.context.sourceForReadinessSetup().id, 'verified');
  f.$('#investigationSource').value = '';
  assert.equal(f.context.sourceForReadinessSetup().id, 'pending');
});

test('editing provider fields invalidates the tested model and any in-flight probe', () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  f.state.modelTested = true; const token = f.state.providerTestToken;
  f.context.markProviderDirty();
  assert.equal(f.state.providerDirty, true);
  assert.equal(f.state.modelTested, false);
  assert.equal(f.state.providerTestToken, token + 1);
  assert.match(f.$('#providerTestStatus').textContent, /Save and run a new/);
  assert.equal(f.$('#trustModel').textContent, 'Model untested');
});

test('workspace loads provider configuration before rendering provider status', async () => {
  const calls = [];
  const context = vm.createContext({
    state: { authenticated: false },
    notice() {}, readiness() {},
    loadSources: async () => {}, loadSkills: async () => {}, loadHistory: async () => {}, loadPolicy: async () => {}, loadHostingGuides: async () => {},
    loadProviderConfiguration: async () => { calls.push('configuration'); },
    loadProvider: async () => { calls.push('status'); },
  });
  const begin = source.indexOf('  async function loadWorkspace()');
  const end = source.indexOf('  async function connectWorkspace(', begin);
  vm.runInContext(source.slice(begin, end), context);
  await context.loadWorkspace();
  assert.deepEqual(calls, ['configuration', 'status']);
});

test('submission refuses a stale tested state while provider edits are unsaved', async () => {
  const f = fixture(); let submitted = false;
  f.state.providerDirty = true; f.state.modelTested = true; f.state.busy = false;
  f.$('#submitRun').disabled = false;
  Object.assign(f.context, {
    guardAsyncFocus: () => () => {}, readiness() {}, openRun: async () => {},
    crypto: { randomUUID: () => 'request-id' },
    api: async () => { submitted = true; return { id: 'run-id' }; },
  });
  const begin = source.indexOf('  async function submitRun(');
  const end = source.indexOf('  async function cancelRun(', begin);
  vm.runInContext(source.slice(begin, end), f.context);
  await f.context.submitRun({ preventDefault() {} });
  assert.equal(submitted, false);
});

test('submission refuses a readiness-approved source while its edits are unsaved', async () => {
  const f = fixture(); let submitted = false;
  f.state.providerDirty = false; f.state.modelTested = true; f.state.busy = false;
  f.state.sourceDirty = true; f.state.sourceEditingId = 'source-a';
  f.$('#investigationSource').value = 'source-a'; f.$('#submitRun').disabled = false;
  Object.assign(f.context, {
    guardAsyncFocus: () => () => {}, readiness() {}, openRun: async () => {},
    crypto: { randomUUID: () => 'request-id' },
    api: async () => { submitted = true; return { id: 'run-id' }; },
  });
  const begin = source.indexOf('  async function submitRun(');
  const end = source.indexOf('  async function cancelRun(', begin);
  vm.runInContext(source.slice(begin, end), f.context);
  await f.context.submitRun({ preventDefault() {} });
  assert.equal(submitted, false);
});

test('vLLM preset does not collide with the default OpsGraph launch port', () => {
  assert.match(source, /vllm: \{ endpoint: 'http:\/\/127\.0\.0\.1:8001\/v1'/);
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

test('model status pill reflects successful and failed connection tests', async () => {
  const success = fixture(); success.context.fillProviderForm({ ...config, revision: 'same' }); success.state.providerDirty = false;
  success.context.api = async path => path.endsWith('/test') ? { ok: true, configuration_revision: 'same' } : path === '/api/providers/current' ? { verification: verification('same') } : { ...config, revision: 'same' };
  await success.context.testProvider();
  assert.equal(success.$('#trustModel').textContent, 'Model reachable');
  assert.equal(success.$('#trustModel').className, 'trust-signal good');

  const failure = fixture(); failure.context.fillProviderForm({ ...config, revision: 'same' }); failure.state.providerDirty = false;
  failure.context.api = async () => { throw new Error('Model offline'); };
  await failure.context.testProvider();
  assert.equal(failure.$('#trustModel').textContent, 'Model unreachable');
  assert.equal(failure.$('#trustModel').className, 'trust-signal failed');
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

test('authenticated status restores only a current matching model verification', async () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  f.context.api = async () => ({ verification: verification('same') });
  await f.context.loadProvider();
  assert.equal(f.state.modelTested, true);
  assert.equal(f.$('#trustModel').textContent, 'Model reachable');
  assert.equal(f.timers.size, 1);
  f.context.api = async () => ({ verification: verification('different') });
  await f.context.loadProvider();
  assert.equal(f.state.modelTested, false);
  assert.equal(f.timers.size, 0);
});

test('expired, failed, checking and invalid verification never mark the model ready', () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  for (const status of ['untested', 'expired', 'failed', 'checking']) {
    f.context.applyProviderVerification({ ...verification('same'), status });
    assert.equal(f.state.modelTested, false);
  }
  for (const seconds of [0, -1, 901, NaN, Infinity]) {
    f.context.applyProviderVerification({ ...verification('same'), valid_for_seconds: seconds });
    assert.equal(f.state.modelTested, false);
  }
});

test('dirty provider fields prevent server verification from turning green', async () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  f.context.markProviderDirty();
  f.context.api = async () => ({ verification: verification('same') });
  await f.context.loadProvider();
  assert.equal(f.state.modelTested, false);
  assert.equal(f.timers.size, 0);
});

test('one-shot expiry clears model readiness and cannot expire a newer check', () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  f.context.applyProviderVerification(verification('same'));
  const old = [...f.timers.values()][0];
  f.context.applyProviderVerification(verification('same'));
  old(); assert.equal(f.state.modelTested, true);
  [...f.timers.values()][0]();
  assert.equal(f.state.modelTested, false);
  assert.equal(f.$('#trustModel').textContent, 'Model check expired');
  assert.equal(f.timers.size, 0);
});

test('late status responses cannot restore an invalidated probe', async () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.loadProvider();
  f.state.providerTestToken++;
  resolve({ verification: verification('same') });
  await pending; assert.equal(f.state.modelTested, false);
});

test('status refresh failure clears earlier verification and its expiry timer', async () => {
  const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
  f.context.applyProviderVerification(verification('same'));
  f.context.api = async () => { throw new Error('Connection lost'); };
  await assert.rejects(f.context.loadProvider(), /Connection lost/);
  assert.equal(f.state.modelTested, false);
  assert.equal(f.timers.size, 0);
});

test('submission refreshes verification before posting and refuses stale or unavailable status', async () => {
  for (const outcome of ['verified', 'expired', 'changed', 'offline']) {
    const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
    f.context.applyProviderVerification(verification('same'));
    f.state.busy = false; f.$('#submitRun').disabled = false;
    f.$('#investigationSource').value = 'source-a';
    f.$('#investigationQuestion').value = 'Count failed payments';
    const calls = [];
    Object.assign(f.context, { openRun: async () => {}, crypto: { randomUUID: () => 'request-id' } });
    f.context.api = async path => {
      calls.push(path);
      if (path === '/api/providers/current') {
        if (outcome === 'offline') throw new Error('Connection lost');
        return { verification: { ...verification(outcome === 'changed' ? 'different' : 'same'), status: outcome === 'expired' ? 'expired' : 'verified' } };
      }
      return { id: 'run-id' };
    };
    const begin = source.indexOf('  async function submitRun(');
    const end = source.indexOf('  async function cancelRun(', begin);
    vm.runInContext(source.slice(begin, end), f.context);
    await f.context.submitRun({ preventDefault() {} });
    assert.deepEqual(calls, outcome === 'verified' ? ['/api/providers/current', '/api/runs'] : ['/api/providers/current']);
    assert.equal(f.state.busy, false);
    if (outcome !== 'verified') assert.equal(f.state.modelTested, false);
  }
});

test('retry refreshes verification and refuses expired, failed, changed or unavailable status', async () => {
  for (const outcome of ['verified', 'expired', 'failed', 'changed', 'offline']) {
    const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
    f.context.applyProviderVerification(verification('same'));
    f.state.run = { id: 'failed-run', source_id: 'source-a', status: 'failed' };
    f.state.busy = false;
    const calls = [];
    f.context.openRun = async () => {};
    f.context.api = async path => {
      calls.push(path);
      if (path === '/api/providers/current') {
        if (outcome === 'offline') throw new Error('Connection lost');
        return { verification: { ...verification(outcome === 'changed' ? 'different' : 'same'), status: ['expired', 'failed'].includes(outcome) ? outcome : 'verified' } };
      }
      return { id: 'retry-run' };
    };
    const begin = source.indexOf('  async function retryRun()');
    const end = source.indexOf('  function newInvestigation()', begin);
    vm.runInContext(source.slice(begin, end), f.context);
    await f.context.retryRun();
    assert.deepEqual(calls, outcome === 'verified' ? ['/api/providers/current', '/api/runs/failed-run/retry'] : ['/api/providers/current']);
    assert.equal(f.state.busy, false);
    assert.equal(f.$('#retryRun').disabled, false);
    if (outcome !== 'verified') assert.equal(f.state.modelTested, false);
  }
});

test('retry refuses dirty provider or source settings even after a successful status refresh', async () => {
  for (const dirty of ['provider', 'source']) {
    const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
    f.state.run = { id: 'failed-run', source_id: 'source-a' };
    f.state.providerDirty = dirty === 'provider';
    f.state.sourceDirty = dirty === 'source'; f.state.sourceEditingId = 'source-a';
    const calls = [];
    f.context.api = async path => { calls.push(path); return { verification: verification('same') }; };
    const begin = source.indexOf('  async function retryRun()');
    const end = source.indexOf('  function newInvestigation()', begin);
    vm.runInContext(source.slice(begin, end), f.context);
    await f.context.retryRun();
    assert.deepEqual(calls, ['/api/providers/current']);
    assert.match(f.$('#runError').textContent, /Review the source settings/);
  }
});

test('disconnect or selecting another run during retry status refresh prevents retry submission', async () => {
  for (const change of ['disconnect', 'run']) {
    const f = fixture(); f.context.fillProviderForm({ ...config, revision: 'same' });
    f.state.run = { id: 'failed-run', source_id: 'source-a' };
    const calls = []; let resolve;
    f.context.api = path => { calls.push(path); return new Promise(done => { resolve = done; }); };
    const begin = source.indexOf('  async function retryRun()');
    const end = source.indexOf('  function newInvestigation()', begin);
    vm.runInContext(source.slice(begin, end), f.context);
    const pending = f.context.retryRun();
    if (change === 'disconnect') { f.state.authEpoch++; f.state.authenticated = false; }
    else f.state.run = { id: 'another-run', source_id: 'source-a' };
    resolve({ verification: verification('same') });
    await pending;
    assert.deepEqual(calls, ['/api/providers/current']);
  }
});
