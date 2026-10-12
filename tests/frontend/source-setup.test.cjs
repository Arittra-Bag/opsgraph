'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { sourceLabel, SourceSetup, readSession, saveSession, verifiedURI, icon, watchWorkspace } = require('../../src/opsgraph/web/static/source-setup.js');
const { createSSEParser } = require('../../src/opsgraph/web/static/events.js');

function storage() {
  const values = new Map();
  return { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key), values };
}
function fixture(request = async () => ({ connections: [] })) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', files: [], hidden: false, disabled: false, textContent: '', dataset: {},
      classList: { toggle() {} }, addEventListener() {}, setAttribute() {}, removeAttribute() {},
      replaceChildren() {}, append() {}, reportValidity: () => true, focus() {},
    });
    return nodes.get(id);
  };
  const document = { getElementById: node, querySelectorAll: () => [], createElement: () => node('created-'+nodes.size), activeElement: null };
  const store = storage(); const calls = [];
  const controller = new SourceSetup({ document, storage: store, request: async (...args) => { calls.push(args); return request(...args); },
    dirty() {}, discovered: async () => {}, source: () => null, authenticated: () => true });
  return { node, store, controller, calls };
}

test('session state stores only validated navigation and saved identifiers, never raw configuration', () => {
  const store = storage(); saveSession(store, { view: 'sources', sourceId: 'db-one', dsn: 'fixture-secret', ca_pem: 'fixture-private-key' });
  assert.deepEqual(readSession(store), { view: 'sources', sourceId: 'db-one' });
  assert.equal([...store.values.values()].join('').includes('fixture-secret'), false);
  saveSession(store, { view: 'javascript:bad', sourceId: '../../other' });
  assert.deepEqual(readSession(store), { view: 'investigations', sourceId: null });
});

test('remote URIs gain verify-full while local proxy transport stays unchanged', () => {
  assert.equal(new URL(verifiedURI('postgresql://reader@remote.example:5432/db')).searchParams.get('sslmode'), 'verify-full');
  assert.equal(verifiedURI('postgresql://reader@127.0.0.1:5432/db'), 'postgresql://reader@127.0.0.1:5432/db');
  assert.equal(verifiedURI('host=db.example port=5432'), 'host=db.example port=5432');
});

test('provider logos use local known assets and unknown names cannot inject markup', () => {
  for (const host of ['local','self_hosted','supabase','neon','aws_rds','google_cloud_sql','azure','digitalocean']) {
    assert.match(icon(host), /^<img.*src="\/assets\/static\/provider-icons\//);
    assert.match(icon(host), /alt="" aria-hidden="true"/);
  }
  assert.equal(icon('constructor'), icon('local'));
  assert.equal(icon('"><script>'), icon('local'));
});

test('host advances to connection and an empty connection cannot skip its save', async () => {
  const f = fixture(); f.controller.go(1); await f.controller.next(); assert.equal(f.controller.step, 2);
  f.node('sourceDsn').reportValidity = () => false;
  await f.controller.next(); assert.equal(f.controller.step, 2); assert.equal(f.calls.length, 0);
});

test('successful connection save clears its password and reuses the discovery callback', async () => {
  const f = fixture(async path => path === '/api/postgres/connections' ? { secret_ref: 'OPSGRAPH_GUI_EXAMPLE_DSN', connections: [{ secret_ref: 'OPSGRAPH_GUI_EXAMPLE_DSN', configured: true }] } : {});
  f.node('sourceDsn').value = 'postgresql://reader:fixture-secret@127.0.0.1:5432/db';
  let discovered = 0; f.controller.discovered = async () => { discovered++; };
  f.controller.go(2); await f.controller.next();
  assert.equal(f.controller.step, 3); assert.equal(f.node('sourceDsn').value, ''); assert.equal(discovered, 1);
  assert.equal([...f.store.values.values()].join('').includes('fixture-secret'), false);
});

test('save failure keeps the connection step and clears the entered password', async () => {
  const f = fixture(async () => { throw new Error('Unavailable'); });
  f.node('sourceDsn').value = 'fixture-secret'; f.controller.go(2); await f.controller.next();
  assert.equal(f.controller.step, 2); assert.equal(f.node('sourceDsn').value, '');
  assert.match(f.node('connectionSaveStatus').textContent, /Re-enter/);
});

test('a stale save cannot change a newly opened source or clear its selected certificate', async () => {
  let resolve; const pending = new Promise(r => { resolve = r; });
  const f = fixture(async (path, options) => options?.method === 'PUT' ? pending : { connections: [] });
  f.node('sourceDsn').value = 'fixture-secret'; f.controller.go(2); const saving = f.controller.next();
  f.controller.start(null); f.node('sourceCaFile').value = 'new-file.pem';
  resolve({ secret_ref: 'OPSGRAPH_GUI_OLD_DSN' }); await saving;
  assert.equal(f.controller.step, 1); assert.equal(f.node('sourceCaFile').value, 'new-file.pem');
  assert.equal(f.node('sourceDsn').disabled, false);
});

test('invalid scope stays at tables instead of skipping inspection', async () => {
  const f = fixture(); f.controller.go(3); f.node('sourceTables').reportValidity = () => false;
  await f.controller.next(); assert.equal(f.controller.step, 3);
});

test('inspection stops for column review before the user advances to the query check', async () => {
  const f = fixture(); f.controller.go(4);
  assert.equal(f.node('sourceNext').hidden, true);
  f.controller.inspected();
  assert.equal(f.controller.step, 4); assert.equal(f.node('sourceNext').hidden, false);
  assert.equal(f.node('sourceNext').textContent, 'Continue to query check');
  await f.controller.next(); assert.equal(f.controller.step, 5);
  f.controller.invalidate(); f.controller.go(4);
  assert.equal(f.node('sourceNext').hidden, true);
});

test('a late startup connection list cannot overwrite a resumed saved connection', async () => {
  let finish; const pending = new Promise(resolve => { finish = resolve; }); let count = 0;
  const f = fixture(async () => ++count === 1 ? pending : { connections: [{ secret_ref: 'SAVED_DSN', configured: true, hosting_profile: 'local' }] });
  f.controller.start(null); await f.controller.resumeConnection('SAVED_DSN');
  finish({ connections: [] }); await pending; await Promise.resolve();
  assert.equal(f.controller.step, 3); assert.equal(f.node('existingConnection').value, 'SAVED_DSN');
});

test('live metadata notifications reuse SSE framing and never submit investigations', async () => {
  let resolve; const changed = new Promise(r => { resolve = r; }); let calls = 0, watcher;
  watcher = watchWorkspace({ fetch: async (url, options) => {
    assert.match(url, /workspace\/events/); assert.equal(options.headers['X-OpsGraph-Key'], 'fixture-key');
    const bytes = new TextEncoder().encode('id: 2\nevent: workspace_changed\ndata: {"revision":2}\n\n');
    return { ok: true, body: new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } }) };
  }, url: revision => '/api/workspace/events?after='+revision, key: () => 'fixture-key', authenticated: () => true,
  parser: createSSEParser, changed: async () => { calls++; resolve(); }, wait: async () => { watcher.stop(); } });
  await changed; watcher.stop(); assert.equal(calls, 1);
});


test('an inspected schema cannot be mistaken for a passing or fresh query check', () => {
  assert.equal(sourceLabel({status:'ready',readiness:{status:'failed'}}),'Query check failed');
  assert.equal(sourceLabel({status:'ready',readiness:{status:'ready'}},false),'Tables inspected');
  assert.equal(sourceLabel({status:'ready',readiness:{status:'ready'}},true),'Query check passed');
  assert.equal(sourceLabel({status:'stale',readiness:{status:'ready'}},true),'Review tables again');
  assert.equal(sourceLabel({status:'configured'}),'Setup needed');
});
