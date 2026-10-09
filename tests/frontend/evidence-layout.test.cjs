'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function markup(item) {
  const context = vm.createContext({ esc: value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]), stamp: value => value || 'Not recorded', json: value => JSON.stringify(value), viewState: { captureStatus: () => 'Saved capture' } });
  vm.runInContext(source.slice(source.indexOf('  function evidenceMarkup('), source.indexOf('  function showEvidence(')), context);
  return context.evidenceMarkup(item, { source_id: 'test' });
}
test('query and rows precede detailed provenance without removing bounds or canonical input', () => {
  const html = markup({ columns: ['id'], rows: [[1]], evidence_hash: 'sha256:result', truncated: true, provenance: { sql: 'SELECT id FROM public.jobs', finished_at: 'captured-time', capture_id: 'capture-1', limits: { max_rows: 100, timeout_ms: 5000 } }, integrity: { format: 'opsgraph-canonical-json-v1', canonical_json: '{"rows":[[1]]}' } });
  assert.ok(html.indexOf('Executed query') < html.indexOf('Captured rows'));
  assert.ok(html.indexOf('Captured rows') < html.indexOf('Capture details and hash input'));
  for (const label of ['Source identity', 'Collection finished', 'Effective query row limit', 'Effective query timeout', 'Truncated']) assert.ok(html.indexOf(label) < html.indexOf('Executed query'));
  assert.match(html, /Yes — additional rows may exist/);
  assert.match(html, /Capture ID/); assert.match(html, /capture-1/);
  assert.match(html, /Inspect exact content-hash input/); assert.match(html, /does not|not capture provenance/);
});
test('untrusted SQL, columns, object values and canonical input remain escaped', () => {
  const html = markup({ columns: ['<img>'], rows: [[{ danger: '<script>' }]], provenance: { sql: '<script>SQL</script>' }, integrity: { format: 'opsgraph-canonical-json-v1', canonical_json: '<script>canonical</script>' } });
  assert.doesNotMatch(html, /<script>|<img>/); assert.match(html, /&lt;script&gt;/); assert.match(html, /&lt;img&gt;/);
});
test('legacy evidence still distinguishes absent SQL and missing canonical input', () => {
  const html = markup({ rows: [{ id: 1 }] });
  assert.match(html, /Exact SQL was not retained/); assert.match(html, /Exact canonical hash input was not retained/); assert.match(html, /Not recorded/);
});
