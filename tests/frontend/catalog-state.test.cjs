'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function fixture() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', textContent: '', innerHTML: '', disabled: false, insertAdjacentHTML() {} });
    return nodes.get(id);
  };
  const state = { authEpoch: 1, authenticated: true, sources: [], skills: [], policy: null, savedSkill: null, skillEditToken: 0 };
  const context = vm.createContext({ $, state, api: async () => ({}), json: JSON.stringify, esc: String, stamp: String, readiness() {}, renderSavedConnection() {}, renderComposerScope() {}, sourceReadinessPassed: item => item?.readiness?.status === 'ready', notice: (id, message = '') => { $(id).textContent = message; }, guardAsyncFocus: () => () => {} });
  vm.runInContext(source.slice(source.indexOf('  async function loadSources()'), source.indexOf('  async function loadHistory()')), context);
  vm.runInContext(source.slice(source.indexOf('  async function loadPolicy()'), source.indexOf('  async function loadWorkspace()')), context);
  vm.runInContext(source.slice(source.indexOf('  async function saveSkill('), source.indexOf("  document.addEventListener('click'")), context);
  return { $, state, context };
}
for (const [method, response, node] of [
  ['loadSources', [{ id: 'source-private', kind: 'postgresql', status: 'configured', name: 'Private source' }], '#sourceCatalog'],
  ['loadSkills', [{ id: 'private-playbook', name: 'Private playbook' }], '#skillCatalog'],
  ['loadPolicy', { allowed_tables: ['public.private'] }, '#policyDetails'],
  ['loadAudit', [{ question: 'Private question' }], '#auditDetails'],
]) {
  test(`${method} discards deferred data after workspace reset`, async () => {
    const f = fixture(); let resolve;
    f.context.api = () => new Promise(done => { resolve = done; });
    const pending = f.context[method]();
    f.state.authEpoch++; f.state.authenticated = false;
    f.$(node).textContent = 'Connect workspace'; f.$(node).innerHTML = '';
    resolve(response); await pending;
    assert.equal(f.$(node).textContent, 'Connect workspace');
    assert.equal(f.$(node).innerHTML, '');
    assert.equal(f.state.sources.length, 0); assert.equal(f.state.skills.length, 0); assert.equal(f.state.policy, null);
  });
}
test('older audit requests cannot overwrite a newer successful refresh', async () => {
  const f = fixture(); const requests = [];
  f.context.api = () => new Promise(done => requests.push(done));
  const first = f.context.loadAudit(); const second = f.context.loadAudit();
  requests[1]([{ question: 'Current question' }]); await second;
  requests[0]([{ question: 'Old question' }]); await first;
  assert.equal(f.$('#auditDetails').textContent, '[{"question":"Current question"}]');
});
test('editing a pending playbook prevents publishing the old validated draft', async () => {
  const f = fixture(); let resolve; let focusCleanup;
  f.context.guardAsyncFocus = () => restore => { focusCleanup = restore; };
  f.$('#skillJson').value = '{"id":"old-playbook"}';
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.saveSkill({ preventDefault() {} });
  f.state.skillEditToken++; f.$('#skillJson').value = '{"id":"edited-playbook"}';
  f.$('#saveSkill').disabled = false; f.$('#skillStatus').textContent = 'Edited';
  resolve({}); await pending;
  assert.equal(f.state.savedSkill, null); assert.equal(f.$('#publishSkill').disabled, true);
  assert.equal(f.$('#skillStatus').textContent, 'Edited'); assert.equal(f.$('#saveSkill').disabled, false);
  assert.equal(focusCleanup, false);
});
test('a failed playbook save cannot repaint the disconnected workspace', async () => {
  const f = fixture(); let reject;
  f.$('#skillJson').value = '{"id":"old-playbook"}';
  f.context.api = () => new Promise((resolve, fail) => { reject = fail; });
  const pending = f.context.saveSkill({ preventDefault() {} });
  f.state.authEpoch++; f.state.authenticated = false; f.$('#skillStatus').textContent = '';
  reject(new Error('Old workspace error')); await pending;
  assert.equal(f.$('#skillStatus').textContent, ''); assert.equal(f.$('#skillError').textContent, '');
  assert.equal(f.state.savedSkill, null);
});
test('publishing captures the approved draft identity and ignores edited-form callbacks', async () => {
  const f = fixture(); let resolve; let url; let refreshed = false; let focusCleanup;
  f.context.guardAsyncFocus = () => restore => { focusCleanup = restore; };
  f.state.savedSkill = 'approved-playbook';
  f.context.api = path => { url = path; return new Promise(done => { resolve = done; }); };
  f.context.loadSkills = async () => { refreshed = true; };
  const pending = f.context.publishSkill();
  f.state.skillEditToken++; f.state.savedSkill = null; f.$('#skillStatus').textContent = 'Edited';
  resolve({}); await pending;
  assert.equal(url, '/api/skills/approved-playbook/publish');
  assert.equal(refreshed, false); assert.equal(f.$('#skillStatus').textContent, 'Edited');
  assert.equal(focusCleanup, false);
});
test('current validated drafts save and publish normally', async () => {
  const f = fixture(); f.$('#skillJson').value = '{"id":"reviewed-playbook"}';
  await f.context.saveSkill({ preventDefault() {} });
  assert.equal(f.state.savedSkill, 'reviewed-playbook'); assert.equal(f.$('#publishSkill').disabled, false);
  f.context.loadSkills = async () => {};
  await f.context.publishSkill();
  assert.equal(f.state.savedSkill, null); assert.match(f.$('#skillStatus').textContent, /published/);
});
