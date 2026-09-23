'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');

function fixture() {
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      value: '', checked: false, textContent: '', innerHTML: '', hidden: false,
      disabled: false, dataset: {},
    });
    return nodes.get(selector);
  };
  const state = {
    sourceSetupToken: 0, roleGuideToken: 0,
    sources: [{ id: 'source-a', kind: 'postgresql', status: 'ready', readiness: { status: 'ready' } }],
  };
  let focusRestored = 0;
  const context = vm.createContext({
    $, state,
    notice: (selector, value = '') => { $(selector).textContent = value; },
    guardAsyncFocus: () => () => { focusRestored++; },
    readiness() {}, invalidateRoleGuide() { state.roleGuideToken++; }, renderInspection() {}, loadSources: async () => {},
    stamp: value => value,
    api: async () => ({}),
  });
  const begin = source.indexOf('  async function saveSource(');
  const end = source.indexOf('  function renderRun(', begin);
  vm.runInContext(source.slice(begin, end), context);
  return { $, state, context, focusRestored: () => focusRestored };
}

test('source save refreshes backend readiness before rendering the new inspection', async () => {
  const f = fixture(); const order = [];
  f.$('#sourceId').value = 'source-a';
  f.$('#sourceName').value = 'Operations';
  f.$('#sourceSecretRef').value = 'OPSGRAPH_SOURCE_DSN';
  f.$('#sourceSchemas').value = 'public';
  f.$('#sourceTables').value = 'public.jobs';
  f.$('#sourceEvidenceBindings').value = '';
  f.context.api = async path => {
    if (path === '/api/sources') return {};
    return { status: 'ready', tables: [{ schema_name: 'public', table_name: 'jobs' }] };
  };
  f.context.loadSources = async () => {
    order.push('load');
    f.state.sources = [{ id: 'source-a', kind: 'postgresql', status: 'ready', readiness: { status: 'pending' } }];
  };
  f.context.renderInspection = () => order.push(`render:${f.state.sources[0].readiness.status}`);
  await f.context.saveSource({ preventDefault() {} });
  assert.deepEqual(order, ['load', 'render:pending']);
  assert.match(f.$('#sourceReadinessStatus').textContent, /^$/);
  assert.equal(f.focusRestored(), 1);
});

test('late readiness response cannot update a different source panel', async () => {
  const f = fixture(); let resolve; let refreshed = 0;
  f.$('#sourceSetup').dataset.sourceId = 'source-a';
  f.$('#sourceId').value = 'source-a';
  f.$('#sourceReadinessTable').value = 'public.jobs';
  f.$('#confirmSourceReadiness').checked = true;
  f.context.api = () => new Promise(done => { resolve = done; });
  f.context.loadSources = async () => { refreshed++; };
  const pending = f.context.runSourceReadiness();
  f.state.sourceSetupToken++;
  f.$('#sourceSetup').dataset.sourceId = 'source-b';
  f.$('#sourceReadinessTable').value = 'public.events';
  f.$('#sourceReadinessStatus').textContent = 'Not run for this source revision.';
  resolve({ checked_at: 'later' });
  await pending;
  assert.equal(f.$('#sourceReadinessStatus').textContent, 'Not run for this source revision.');
  assert.equal(refreshed, 0);
  assert.equal(f.focusRestored(), 0);
});

test('late readiness response is discarded when the selected table changes', async () => {
  const f = fixture(); let resolve;
  f.$('#sourceSetup').dataset.sourceId = 'source-a';
  f.$('#sourceId').value = 'source-a';
  f.$('#sourceReadinessTable').value = 'public.jobs';
  f.$('#confirmSourceReadiness').checked = true;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.runSourceReadiness();
  f.$('#sourceReadinessTable').value = 'public.events';
  f.$('#sourceReadinessStatus').textContent = 'Selection changed.';
  resolve({ checked_at: 'later' });
  await pending;
  assert.equal(f.$('#sourceReadinessStatus').textContent, 'Selection changed.');
});

test('readiness turns green only after refreshed source state confirms the same result', async () => {
  const f = fixture();
  const result = { checked_at: '2026-09-22T10:00:00Z', source_revision: 'revision-a' };
  f.$('#sourceSetup').dataset.sourceId = 'source-a';
  f.$('#sourceId').value = 'source-a';
  f.$('#sourceReadinessTable').value = 'public.jobs';
  f.$('#confirmSourceReadiness').checked = true;
  f.context.api = async () => result;
  f.context.loadSources = async () => {
    f.state.sources = [{
      id: 'source-a', kind: 'postgresql', status: 'ready',
      readiness: { status: 'ready', checked_at: result.checked_at, source_revision: result.source_revision },
    }];
  };
  f.context.sourceReadinessPassed = item => item?.status === 'ready' && item.readiness?.status === 'ready';
  await f.context.runSourceReadiness();
  assert.match(f.$('#sourceReadinessStatus').textContent, /^Passed/);
  assert.equal(f.$('#confirmSourceReadiness').checked, false);
});

test('source list defaults to a readiness-checked source', async () => {
  const f = fixture();
  const ready = [
    { id: 'pending', name: 'Pending', kind: 'postgresql', status: 'ready', readiness: { status: 'pending' }, allowed_tables: [] },
    { id: 'verified', name: 'Verified', kind: 'postgresql', status: 'ready', readiness: { status: 'ready' }, allowed_tables: [] },
  ];
  Object.assign(f.context, {
    sourceReadinessPassed: item => item?.status === 'ready' && item.readiness?.status === 'ready',
    sourceReady: item => item?.kind === 'postgresql' && item.status === 'ready',
    esc: value => String(value),
  });
  f.context.api = async () => ready;
  f.context.stamp = value => value;
  const begin = source.indexOf('  async function loadSources()');
  const end = source.indexOf('  async function loadSkills()', begin);
  vm.runInContext(source.slice(begin, end), f.context);
  await f.context.loadSources();
  assert.equal(f.$('#investigationSource').value, 'verified');
});
