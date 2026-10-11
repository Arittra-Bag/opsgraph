'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/work-outcomes.js', 'utf8');
const app = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function fixture() {
  class Node {
    constructor(tag = '') { this.tag = tag; this.value = ''; this.children = []; this.listeners = {}; this.disabled = false; this.hidden = false; this.checked = false; this.textContent = ''; }
    get textContent() { return this.text || ''; }
    set textContent(value) { this.text = value; this.children = []; }
    append(...values) { this.children.push(...values); }
    replaceChildren(...values) { this.textContent = ''; this.children = values; }
    get firstChild() { return this.children[0]; }
    addEventListener(type, handler) { this.listeners[type] = handler; }
    querySelectorAll() { return this.children.filter(n => n.tag === 'button'); }
    reset() {}
  }
  const nodes = new Map(); const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const doc = { getElementById: $, createElement: tag => new Node(tag) };
  const context = vm.createContext({ window: {}, document: doc, Date, Map, Set, setTimeout, clearTimeout });
  vm.runInContext(source, context);
  const calls = [], scheduled = [], downloads = []; let auth = true;
  let reply = path => path.includes('contracts') ? [{ definition: { id: 'customer-exports', version: 1, source_id: 'exports-db' } }] : [];
  const api = context.window.OpsGraphWorkOutcomes;
  const mounted = api.mount({ document: doc, request: async (path, options) => { calls.push({ path, options }); return reply(path, options); }, authenticated: () => auth, openRun() {}, download: (...args) => downloads.push(args), schedule: fn => { scheduled.push(fn); return scheduled.length; }, unschedule() {} });
  return { $, api, mounted, calls, scheduled, downloads, setReply: fn => { reply = fn; }, disconnect: () => { auth = false; mounted.reset(); } };
}
const input = { contract_id: 'customer-exports', version: 1, tenant_id: 'tenant-a', start: '2026-10-11T11:00:00.000Z', end: '2026-10-11T12:00:00.000Z', as_of: '2026-10-11T12:00:00.000Z' };
function calculation(overdue = 12, examples = []) {
  return { scope: { ...input }, contract: { source_id: 'exports-db' }, facts: { population: 1000, affected_customers: 8, orphan_events: 0, unassignable_events: 0, counts: { ready: 930, cancelled: 20, terminal_failure: 5, pending_within_deadline: 30, overdue_unresolved: overdue, unknown: 3 }, examples, overdue_examples_complete: true } };
}
function saved(calc = calculation(), status = 'completed') { return { id: 'run-test', work_outcome: input, status, answer: status === 'completed' ? { work_outcome: calc, limitations: ['Source coverage is a premise.'] } : null, evidence: [] }; }
const flush = () => new Promise(resolve => setImmediate(resolve));
test('UTC input does not depend on local timezone and rejects normalized invalid dates', () => {
  const f = fixture(); assert.equal(f.api.utc('2026-10-11T12:00'), '2026-10-11T12:00:00.000Z');
  for (const value of ['2026-02-30T12:00', '', '2026-10-11T12:00Z', '2026-10-11T24:00']) assert.throws(() => f.api.utc(value));
});
test('invalid ordering, blank tenant and a window over 31 days never create a scope', () => {
  const f = fixture(); const contract = { id: 'customer-exports', version: 1 };
  const values = { tenant: 'tenant-a', start: '2026-10-11T11:00', end: '2026-10-11T12:00', cutoff: '2026-10-11T12:00' };
  for (const patch of [{ tenant: ' ' }, { cutoff: '2026-10-11T11:30' }, { end: values.start }, { start: '2026-09-01T11:00' }]) assert.throws(() => f.api.scope(contract, { ...values, ...patch }));
});
test('both deferred refresh responses and failures are discarded after reset', async () => {
  for (const reject of [false, true]) {
    const f = fixture(); const pending = [];
    f.setReply(() => new Promise((resolve, fail) => pending.push({ resolve, fail })));
    const connect = f.mounted.connect(); f.disconnect();
    for (const p of pending) reject ? p.fail(new Error('Old private error')) : p.resolve([]);
    await connect;
    assert.equal(f.$('mwStatus').textContent, 'Connect workspace to load reviewed export rules.');
    assert.equal(f.$('mwError').textContent, ''); assert.equal(f.$('mwHistory').textContent, 'No audits loaded.');
  }
});
test('catalog failures stay in the audit view and a fresh refresh can recover', async () => {
  const f = fixture();
  f.setReply(async () => { throw new Error('Audit catalog unavailable'); });
  await f.mounted.connect();
  assert.equal(f.$('mwError').textContent, 'Audit catalog unavailable');
  assert.equal(f.$('mwRefresh').disabled, false);
  f.setReply(path => path.includes('contracts') ? [{ definition: { id: 'customer-exports', version: 1, source_id: 'exports-db' } }] : []);
  await f.mounted.refresh();
  assert.equal(f.$('mwError').textContent, '');
  assert.match(f.$('mwStatus').textContent, /1 reviewed export rule set/);
});
test('recheck freezes the saved cohort even when input fields are edited', async () => {
  const f = fixture(); await f.mounted.connect(); f.setReply(path => path === '/api/runs' ? saved() : saved());
  await f.mounted.load('run-test');
  f.$('mwTenant').value = 'wrong-tenant'; f.$('mwStart').value = '2026-01-01T00:00'; f.$('mwLater').value = '2026-10-11T12:05';
  f.$('mwRecheckForm').listeners.submit({ preventDefault() {} }); await flush();
  const body = JSON.parse(f.calls.find(item => item.path === '/api/runs' && item.options)?.options.body);
  assert.equal(body.parent_run_id, 'run-test'); assert.equal(body.source_id, 'exports-db');
  assert.deepEqual(body.work_outcome, { ...input, as_of: '2026-10-11T12:05:00.000Z' });
});
test('empty contract selection cannot silently choose the first definition', async () => {
  const f = fixture(); await f.mounted.connect(); f.$('mwContract').value = '';
  f.$('mwForm').listeners.submit({ preventDefault() {} }); await flush();
  assert.match(f.$('mwError').textContent, /Select reviewed export rules/); assert.equal(f.calls.some(c => c.options?.method === 'POST'), false);
});
test('stale run responses and scheduled polls cannot repopulate forgotten evidence', async () => {
  const f = fixture(); await f.mounted.connect(); let resolve;
  f.setReply(() => new Promise(done => { resolve = done; }));
  const pending = f.mounted.load('run-test'); f.disconnect(); resolve(saved()); await pending;
  assert.equal(f.$('mwResult').hidden, true); assert.equal(f.$('mwEvidence').textContent, ''); assert.equal(f.scheduled.length, 0);
});
test('bounded lists show full totals and never claim missing examples are zero overdue', async () => {
  const f = fixture(); await f.mounted.connect(); const calc = calculation(70, []); calc.facts.overdue_examples_complete = false;
  f.setReply(() => saved(calc)); await f.mounted.load('run-test');
  assert.match(f.$('mwSampleNote').textContent, /0 of 70/); assert.match(f.$('mwSampleNote').textContent, /not a complete/);
  assert.equal(f.$('mwCounts').children[0].children[0].textContent, '70');
});
test('captured strings render as text and never create executable markup', async () => {
  const f = fixture(); await f.mounted.connect(); const payload = '<img src=x onerror=alert(1)>';
  const calc = calculation(1, [{ promise_id: payload, customer_id: payload, due_at: '2026-10-11T11:10:00Z', state: 'overdue_unresolved' }]);
  f.setReply(() => saved(calc)); await f.mounted.load('run-test');
  assert.equal(f.$('mwExamples').children[0].children[0].textContent, payload);
  assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML/);
});
test('deadline display honors its UTC label without rewriting captured evidence', async () => {
  const f = fixture(); await f.mounted.connect();
  const calc = calculation(1, [{ promise_id: 'p', customer_id: 'c', due_at: '2026-10-11T16:40:00+05:30', state: 'overdue_unresolved' }]);
  f.setReply(() => saved(calc)); await f.mounted.load('run-test');
  assert.equal(f.$('mwExamples').children[0].children[2].textContent, '2026-10-11T11:10:00.000Z');
  assert.match(f.$('mwEvidence').textContent, /16:40:00\+05:30/);
});
test('reset stops scheduled polling before another API request', async () => {
  const f = fixture(); await f.mounted.connect(); f.setReply(() => saved(null, 'running'));
  await f.mounted.load('run-test'); const count = f.calls.length; assert.equal(f.scheduled.length, 1);
  f.disconnect(); await f.scheduled[0](); assert.equal(f.calls.length, count);
});
test('recovery comparisons require same cohort and visible membership, not just count subtraction', () => {
  const f = fixture(); const before = calculation(12, [{ promise_id: 'p', state: 'overdue_unresolved' }]); const after = calculation(0, [{ promise_id: 'p', state: 'cancelled' }]); after.scope.as_of = '2026-10-11T12:05:00Z';
  assert.match(f.api.comparison(before, after), /0 previously overdue export requests are now recorded as ready/);
  after.facts.examples = []; assert.match(f.api.comparison(before, after), /At least 0/);
  after.facts.population = 1001; assert.match(f.api.comparison(before, after), /number of accepted export requests changed/);
  after.scope.tenant_id = 'tenant-b'; assert.equal(f.api.comparison(before, after), '');
});
test('failed audits show their saved error and cannot recheck an absent calculation', async () => {
  const f = fixture(); await f.mounted.connect(); const result = saved(null, 'blocked'); result.error = { message: 'Review source coverage.' }; f.setReply(() => result);
  await f.mounted.load('run-test'); assert.equal(f.$('mwRecheckFields').disabled, true); assert.match(f.$('mwError').textContent, /Review source coverage/);
});
test('reset prevents a pending JSON export from downloading old workspace data', async () => {
  const f = fixture(); await f.mounted.connect(); f.setReply(() => saved()); await f.mounted.load('run-test'); let resolve; f.setReply(() => new Promise(done => { resolve = done; }));
  const pending = f.$('mwExport').listeners.click(); f.disconnect(); resolve({ private: true }); await pending;
  assert.equal(f.downloads.length, 0);
});
test('JSON download keeps its object URL alive until the browser can consume it', () => {
  const callbacks = [], revocations = []; let clicked = false;
  const anchor = { click() { clicked = true; }, remove() {} };
  const context = vm.createContext({ json: JSON.stringify, Blob, URL: { createObjectURL: () => 'blob:local', revokeObjectURL: url => revocations.push(url) }, document: { createElement: () => anchor, body: { append() {} } }, setTimeout: fn => callbacks.push(fn) });
  vm.runInContext(app.slice(app.indexOf('  function download('), app.indexOf('  function stopStream(')), context);
  context.download('audit.json', { population: 1000 }); assert.equal(clicked, true); assert.equal(anchor.download, 'audit.json'); assert.equal(revocations.length, 0);
  callbacks[0](); assert.deepEqual(revocations, ['blob:local']);
});
test('saved contract audits do not inherit a fictitious model stage count', () => {
  const nodes = new Map(); const $ = id => { if (!nodes.has(id)) nodes.set(id, { textContent: '', innerHTML: '', dataset: {} }); return nodes.get(id); };
  const context = vm.createContext({ $, state: { run: { id: 'run-test', status: 'completed', configuration: { execution_kind: 'work_outcome' }, evidence: [{}] }, runEvents: [] }, viewState: { stageProgress: () => ({}) } });
  vm.runInContext(app.slice(app.indexOf('  function renderExecutionProgress('), app.indexOf('  function renderComposerScope(')), context);
  context.renderExecutionProgress(); assert.match($('#stageSummary').textContent, /no model invocation · 1 capture/); assert.doesNotMatch($('#stageSummary').textContent, /0 of 4/);
});

test('completed recheck advances the next suggestion but preserves an explicitly later time', async () => {
  const f = fixture(); await f.mounted.connect(); let result = saved();
  f.setReply(() => result); await f.mounted.load('run-test');
  assert.equal(f.$('mwLater').value, '2026-10-11T12:05:00');
  const later = calculation(0); later.scope.as_of = '2026-10-11T12:05:00.000Z';
  result = { ...saved(later), id: 'run-next', work_outcome: later.scope };
  f.$('mwRecheckForm').listeners.submit({ preventDefault() {} }); await flush();
  assert.equal(f.$('mwLater').value, '2026-10-11T12:10:00');
  f.$('mwLater').value = '2026-10-11T13:00';
  f.$('mwRecheckForm').listeners.submit({ preventDefault() {} }); await flush();
  assert.equal(f.$('mwLater').value, '2026-10-11T13:00');
});
test('terminal results enter history without refresh, distinguish duplicate scopes and deduplicate IDs', async () => {
  const f = fixture(); await f.mounted.connect(); let result = saved(); f.setReply(() => result);
  await f.mounted.load('run-test'); assert.equal(f.$('mwHistory').children.length, 1);
  await f.mounted.load('run-test'); assert.equal(f.$('mwHistory').children.length, 1);
  result = { ...saved(), id: 'run-other' }; await f.mounted.load('run-other');
  assert.equal(f.$('mwHistory').children.length, 2);
  assert.notEqual(f.$('mwHistory').children[0].textContent, f.$('mwHistory').children[1].textContent);
  f.disconnect(); assert.equal(f.$('mwHistory').children.length, 0);
});
test('late terminal result cannot enter history after workspace reset', async () => {
  const f = fixture(); await f.mounted.connect(); let resolve;
  f.setReply(() => new Promise(done => { resolve = done; }));
  const pending = f.mounted.load('run-test'); f.disconnect(); resolve(saved()); await pending;
  assert.equal(f.$('mwHistory').children.length, 0);
  assert.equal(f.$('mwHistory').textContent, 'No audits loaded.');
});
test('same-time recheck names the saved UTC time without creating a run', async () => {
  const f = fixture(); await f.mounted.connect(); f.setReply(() => saved()); await f.mounted.load('run-test');
  f.$('mwLater').value = '2026-10-11T12:00';
  f.$('mwRecheckForm').listeners.submit({ preventDefault() {} }); await flush();
  assert.match(f.$('mwError').textContent, /after 2026-10-11T12:00:00.000Z \(UTC\)/);
  assert.equal(f.calls.some(c => c.options?.method === 'POST'), false);
});
test('plain limitations and summary do not rewrite the saved calculation, answer or export', async () => {
  const f = fixture(); await f.mounted.connect(); const result = saved();
  result.answer.summary = '12 overdue unresolved exports across 8 customers; 5 terminal failures and 3 unknown among 1000 accepted promises.';
  result.answer.limitations = ['Outcome definitions and source coverage are operator-reviewed premises, not verified business truth.', 'Ready database records do not prove customer access or an incident cause.'];
  const original = JSON.stringify(result); f.setReply(() => result); await f.mounted.load('run-test');
  assert.match(f.$('mwLimitations').children[0].textContent, /cannot verify those assumptions/);
  assert.match(f.api.readable(result.answer.summary), /accepted export requests/);
  assert.equal(f.api.readable('constructor'), 'constructor');
  assert.match(f.$('mwLimitations').children[1].textContent, /does not prove the customer can open/);
  await f.$('mwExport').listeners.click(); assert.equal(JSON.stringify(f.downloads[0][1]), original);
  assert.equal(JSON.stringify(result), original);
  assert.deepEqual(JSON.parse(f.$('mwEvidence').textContent).calculation, result.answer.work_outcome);
});
