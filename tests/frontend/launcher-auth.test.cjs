const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/opsgraph/web/static/app.js'), 'utf8');
const helper = source.match(/  async function startWorkspace\(\) \{[\s\S]*?(?=\n  readiness\(\); loadBootstrap)/)?.[0];
assert.ok(helper);
function fixture() {
  const state = { authEpoch: 0 }, items = new Map(), calls = [];
  let resolveFetch;
  const f = vm.runInNewContext(`(${helper.trim()})`, {
    state, launchToken: 'fixture-token',
    validWorkspaceKey: value => typeof value === 'string' && /^[\x20-\x7e]+$/.test(value) && Boolean(value.trim()),
    fetch: () => new Promise(resolve => { resolveFetch = resolve; }),
    sessionStorage: { setItem: (k,v) => items.set(k,v), getItem: k => items.get(k) },
    key: () => items.get('opsgraph.workspaceKey'),
    loadWorkspace: async () => calls.push('load'), openRun: async () => calls.push('open'),
    notice: () => calls.push('notice'), readiness: () => calls.push('ready'),
  });
  return { state, items, calls, f, respond: () => resolveFetch({ok:true,json:async()=>({key:'fixture-key'})}) };
}
test('disconnect during launcher exchange cannot restore credentials or authenticated history', async () => {
  const f=fixture(); const pending=f.f(); f.state.authEpoch++; f.respond(); await pending;
  assert.equal(f.items.size,0); assert.deepEqual(f.calls,[]);
});
test('unchanged launcher exchange connects once and loads the workspace', async () => {
  const f=fixture(); const pending=f.f(); f.respond(); await pending;
  assert.equal(f.items.get('opsgraph.workspaceKey'),'fixture-key'); assert.deepEqual(f.calls,['load']);
});
