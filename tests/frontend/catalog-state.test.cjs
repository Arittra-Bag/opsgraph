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
  vm.runInContext(source.slice(source.indexOf('  function queueSkillMutation('), source.indexOf("  document.addEventListener('click'")), context);
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
for (const method of ['loadSources', 'loadSkills', 'loadPolicy', 'loadAudit']) {
  for (const invalidation of ['workspace', 'newer request']) {
    test(`${method} ignores stale rejection after ${invalidation}`, async () => {
      const f = fixture(); const requests = [];
      f.context.api = () => new Promise((resolve, reject) => requests.push({ resolve, reject }));
      const first = f.context[method]();
      let second;
      if (invalidation === 'workspace') f.state.authEpoch++;
      else { second = f.context[method](); requests[1].resolve(method === 'loadPolicy' ? { id: 'current' } : []); await second; }
      requests[0].reject(new Error('Obsolete backend error'));
      await assert.doesNotReject(first);
    });
  }
  test(`${method} preserves current request failures for caller handling`, async () => {
    const f = fixture(); f.context.api = async () => { throw new Error('Current backend error'); };
    await assert.rejects(f.context[method](), /Current backend error/);
  });
}
function mutationFixture() {
  const f = fixture(); const requests = [];
  f.context.api = (path, options) => new Promise((resolve, reject) => requests.push({ path, body: options.body ? JSON.parse(options.body) : null, resolve, reject }));
  const save = version => {
    f.state.skillEditToken++; f.state.savedSkill = null;
    f.$('#skillJson').value = JSON.stringify({ id: 'same-playbook', version });
    return f.context.saveSkill({ preventDefault() {} });
  };
  return { ...f, requests, save };
}
test('same-ID draft saves are serialized and retain the newly submitted definition', async () => {
  const f = mutationFixture();
  const older = f.save('1'); const newer = f.save('2');
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].body.version, '1');
  f.requests[0].resolve({}); await older; await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2); assert.equal(f.requests[1].body.version, '2');
  assert.equal(f.state.savedSkill, null); assert.equal(f.$('#publishSkill').disabled, true);
  f.requests[1].resolve({}); await newer;
  assert.equal(f.state.savedSkill, 'same-playbook'); assert.equal(f.$('#publishSkill').disabled, false);
});
test('obsolete queued definitions are skipped before a backend request starts', async () => {
  const f = mutationFixture();
  const older = f.save('1'); const obsolete = f.save('2'); const latest = f.save('3');
  f.requests[0].resolve({}); await older; await obsolete; await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2); assert.equal(f.requests[1].body.version, '3');
  f.requests[1].resolve({}); await latest;
  assert.equal(f.state.savedSkill, 'same-playbook');
});
test('a settled failed save does not poison the queue or overwrite the latest form', async () => {
  const f = mutationFixture();
  const older = f.save('1'); const newer = f.save('2');
  f.requests[0].reject(new Error('Old request rejected')); await older; await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2); assert.equal(f.$('#skillError').textContent, '');
  f.requests[1].resolve({}); await newer;
  assert.equal(f.state.savedSkill, 'same-playbook');
});
test('workspace reset prevents queued saves from reaching the backend', async () => {
  const f = mutationFixture();
  const older = f.save('1'); const queued = f.save('2');
  f.state.authEpoch++; f.state.authenticated = false;
  f.requests[0].resolve({}); await older; await queued;
  assert.equal(f.requests.length, 1); assert.equal(f.state.savedSkill, null);
});
test('a new draft save waits for the approved publication request to finish', async () => {
  const f = mutationFixture(); f.state.savedSkill = 'same-playbook';
  const publishing = f.context.publishSkill(); const saving = f.save('2');
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].path, '/api/skills/same-playbook/publish');
  f.requests[0].resolve({}); await publishing; await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.length, 2); assert.equal(f.requests[1].path, '/api/skills/drafts');
  f.requests[1].resolve({}); await saving;
  assert.equal(f.state.savedSkill, 'same-playbook'); assert.match(f.$('#skillStatus').textContent, /Validated draft saved/);
});
