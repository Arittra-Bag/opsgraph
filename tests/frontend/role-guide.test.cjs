'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');

function fixture() {
  const nodes = new Map();
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, { value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {} });
    return nodes.get(selector);
  };
  const requests = [];
  const state = { sourceSetupToken: 1, roleGuideToken: 0 };
  const context = vm.createContext({
    $, state,
    esc: value => String(value),
    notice: (selector, value = '') => { $(selector).textContent = value; },
    api: async (path, options) => {
      requests.push({ path, body: JSON.parse(options.body) });
      return {
        executed: false,
        allowed_tables: ['public.jobs'],
        excluded_tables: [],
        sql: 'GRANT SELECT ON TABLE "public"."jobs" TO "opsgraph_reader";',
        password_setup: ['Set the password interactively.'],
        notes: ['Nothing was executed.'],
      };
    },
    navigator: { clipboard: { writeText: async () => {} } },
  });
  const begin = source.indexOf('  function roleGuideInputs()');
  const end = source.indexOf('  function renderInspection(', begin);
  vm.runInContext(source.slice(begin, end), context);
  return { $, state, context, requests };
}

test('role guide sends exact table scope and renders returned SQL as text', async () => {
  const f = fixture();
  f.$('#sourceTables').value = 'public.jobs';
  f.$('#roleGuideName').value = 'opsgraph_reader';
  f.$('#roleGuideDatabase').value = 'operations';
  await f.context.generateRoleGuide();
  assert.deepEqual(f.requests, [{
    path: '/api/postgres/role-guide',
    body: { role_name: 'opsgraph_reader', database: 'operations', tables: ['public.jobs'] },
  }]);
  assert.match(f.$('#roleGuideOutput code').textContent, /GRANT SELECT/);
  assert.equal(f.$('#roleGuideOutput').hidden, false);
  assert.match(f.$('#roleGuideStatus').textContent, /nothing executed/);
});

test('role guide requires an explicit table before calling the backend', async () => {
  const f = fixture();
  await f.context.generateRoleGuide();
  assert.equal(f.requests.length, 0);
  assert.match(f.$('#roleGuideError').textContent, /at least one/);
});

test('editing role-guide inputs clears generated SQL and invalidates a late response', async () => {
  const f = fixture();
  f.$('#sourceTables').value = 'public.jobs';
  f.$('#roleGuideName').value = 'opsgraph_reader';
  let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.generateRoleGuide();
  f.$('#roleGuideName').value = 'another_reader';
  f.context.invalidateRoleGuide();
  resolve({
    allowed_tables: ['public.jobs'], excluded_tables: [],
    sql: 'SHOULD NOT RENDER', password_setup: [], notes: [],
  });
  await pending;
  assert.equal(f.$('#roleGuideOutput code').textContent, '');
  assert.equal(f.$('#roleGuideOutput').hidden, true);
  assert.equal(f.$('#copyRoleGuide').hidden, true);
  assert.equal(f.$('#generateRoleGuide').disabled, false);
  assert.equal(f.$('#roleGuideStatus').textContent, 'No script generated.');
});
