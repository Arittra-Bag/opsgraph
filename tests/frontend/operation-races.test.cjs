'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function fixture() {
  const request = deferred(), nodes = new Map(), rendered = [], opened = [], errors = [], focus = [], calls = [];
  const state = { authenticated: true, authEpoch: 1, streamToken: 3, busy: false, modelTested: true, run: { id: 'inv-original', source_id: 'source-a', status: 'running' } };
  const $ = key => { if (!nodes.has(key)) nodes.set(key, { disabled: false }); return nodes.get(key); };
  const context = vm.createContext({
    state, $, terminal: status => ['completed', 'failed', 'cancelled'].includes(status),
    guardAsyncFocus: () => restore => focus.push(restore), readiness: () => {},
    notice: (key, message) => { if (message) errors.push(message); },
    loadProvider: async () => {},
    api: async (path, options) => { calls.push({ path, options }); return request.promise; },
    renderRun: run => { rendered.push(run); state.run = run; },
    openRun: async id => { opened.push(id); state.streamToken++; state.run = { id, retry_of: 'inv-original', status: 'queued' }; },
  });
  vm.runInContext(source.slice(source.indexOf('  async function cancelRun()'), source.indexOf('  function newInvestigation()')), context);
  return { state, $, context, request, rendered, opened, errors, focus, calls };
}
const move = {
  investigation: f => { f.state.streamToken++; f.state.run = { id: 'inv-other', status: 'completed' }; },
  workspace: f => { f.state.authEpoch++; f.state.streamToken++; f.state.run = null; },
  reopened: f => { f.state.streamToken++; },
};
for (const [change, navigate] of Object.entries(move)) {
  for (const action of ['cancel', 'retry']) {
    for (const outcome of ['success', 'failure']) {
      test(`${action} ${outcome} after ${change} navigation cannot replace the selection, error or focus`, async () => {
        const f = fixture();
        if (action === 'retry') f.state.run.status = 'failed';
        const pending = f.context[`${action}Run`]();
        await new Promise(setImmediate);
        assert.equal(f.calls.length, 1);
        navigate(f); const selected = f.state.run;
        if (outcome === 'success') f.request.resolve({ id: action === 'retry' ? 'inv-retry' : 'inv-original', status: 'cancelled' });
        else f.request.reject(new Error('Obsolete operation error'));
        await pending;
        assert.equal(f.state.run, selected);
        assert.equal(f.rendered.length, 0); assert.equal(f.opened.length, 0); assert.equal(f.errors.length, 0);
        assert.deepEqual(f.focus, [false]);
        if (action === 'retry') assert.equal(f.state.busy, false);
      });
    }
  }
}
test('current cancellation remains applied when SSE refreshed the selected run object', async () => {
  const f = fixture(); const pending = f.context.cancelRun();
  f.state.run = { ...f.state.run, status: 'cancelling' };
  const result = { id: 'inv-original', status: 'cancelled' };
  f.request.resolve(result); await pending;
  assert.equal(f.state.run, result); assert.equal(f.rendered.length, 1);
  assert.equal(f.calls[0].path, '/api/runs/inv-original/cancel'); assert.deepEqual(f.focus, [true]);
});
test('current cancellation failure stays visible and allows another cancellation request', async () => {
  const f = fixture(); const pending = f.context.cancelRun();
  f.request.reject(new Error('Current failure')); await pending;
  assert.deepEqual(f.errors, ['Current failure']); assert.equal(f.$('#cancelRun').disabled, false);
  assert.deepEqual(f.focus, [true]);
});
test('current retry opens the accepted attempt and releases submission controls', async () => {
  const f = fixture(); f.state.run.status = 'failed';
  const pending = f.context.retryRun(); await new Promise(setImmediate);
  f.request.resolve({ id: 'inv-retry', status: 'queued' }); await pending;
  assert.deepEqual(f.opened, ['inv-retry']); assert.equal(f.state.run.id, 'inv-retry');
  assert.equal(f.calls[0].path, '/api/runs/inv-original/retry');
  assert.equal(f.state.busy, false); assert.equal(f.$('#retryRun').disabled, false); assert.deepEqual(f.focus, [true]);
});
test('current retry failure remains visible without opening another attempt', async () => {
  const f = fixture(); f.state.run.status = 'failed';
  const pending = f.context.retryRun(); await new Promise(setImmediate);
  f.request.reject(new Error('Current retry failure')); await pending;
  assert.deepEqual(f.errors, ['Current retry failure']); assert.equal(f.opened.length, 0);
  assert.equal(f.state.busy, false); assert.deepEqual(f.focus, [true]);
});
test('navigation while provider readiness refreshes stops retry submission', async () => {
  const f = fixture(), provider = deferred(); f.state.run.status = 'failed';
  f.context.loadProvider = () => provider.promise;
  const pending = f.context.retryRun(); move.investigation(f); provider.resolve(); await pending;
  assert.equal(f.calls.length, 0); assert.equal(f.state.run.id, 'inv-other'); assert.equal(f.state.busy, false);
  assert.deepEqual(f.focus, [false]);
});
