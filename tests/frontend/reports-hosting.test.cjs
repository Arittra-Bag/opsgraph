'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function fixture() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', checked: false, hidden: false, disabled: false, textContent: '', innerHTML: '', children: [], setAttribute() {}, removeAttribute() {}, replaceChildren() { this.children = []; }, append(item) { this.children.push(item); } });
    return nodes.get(id);
  };
  const state = { authenticated: true, authEpoch: 0, hostingToken: 0, hostingGuides: [], sourceDirty: false, reportToken: 0, reportRunId: 'run-a', run: { id: 'run-a', updated_at: 'saved-time' }, activeDrawer: { id: 'reportDrawer' } };
  const context = vm.createContext({ $, state, URL, preserveFocus: update => update(), readiness() {}, document: { createElement: tag => ({ tag, textContent: '', children: [], append(...items) { this.children.push(...items); } }) }, esc: value => String(value).replace(/</g, '&lt;'), stamp: value => value, runId: value => value, sourceReadinessPassed: item => item?.readiness?.status === 'ready', notice: (id, value = '') => { $(id).textContent = value; }, api: async () => ({}) });
  vm.runInContext(source.slice(source.indexOf('  function renderHostingGuide()'), source.indexOf('  async function loadSources()')), context);
  return { $, state, context };
}
const report = { run_id: 'run-a', snapshot_updated_at: 'saved-time', run_status: 'completed', markdown: '<img onerror=alert(1)>', filename: 'run-a-report.md' };

test('report preview renders as text and requires explicit review', async () => {
  const f = fixture(); let request;
  f.$('#reportQuestion').checked = true;
  f.context.api = async (path, options) => { request = { path, body: JSON.parse(options.body) }; return report; };
  await f.context.generateReport({ preventDefault() {} });
  assert.equal(request.path, '/api/runs/run-a/report');
  assert.equal(request.body.include_question, true); assert.equal(request.body.include_rows, false);
  assert.equal(f.$('#reportPreview').textContent, report.markdown);
  assert.equal(f.$('#reportPreviewPanel').hidden, false);
  assert.equal(f.$('#downloadReport').disabled, true);
  assert.equal(f.context.shareableReport(), false);
  f.$('#confirmReportReview').checked = true;
  assert.equal(Boolean(f.context.shareableReport()), true);
});

test('editing selections clears sensitive preview and invalidates sharing', async () => {
  const f = fixture(); f.context.api = async () => report;
  await f.context.generateReport(); f.$('#confirmReportReview').checked = true;
  f.context.resetReport();
  assert.equal(f.state.report, null); assert.equal(f.$('#reportPreview').textContent, '');
  assert.equal(f.$('#confirmReportReview').checked, false);
  assert.equal(f.$('#downloadReport').disabled, true);
});

for (const change of ['selection', 'workspace', 'run', 'drawer']) test(`late report response is discarded after ${change} changes`, async () => {
  const f = fixture(); let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.generateReport();
  if (change === 'selection') f.context.resetReport();
  if (change === 'workspace') f.state.authEpoch++;
  if (change === 'run') f.state.run.id = 'run-b';
  if (change === 'drawer') f.state.activeDrawer = null;
  resolve(report); await pending;
  assert.equal(f.state.report, null); assert.equal(f.$('#reportPreview').textContent, '');
});

test('changed saved snapshot cannot be shared through a reviewed old preview', async () => {
  const f = fixture(); f.context.api = async () => report;
  await f.context.generateReport(); f.$('#confirmReportReview').checked = true;
  f.state.run.updated_at = 'new-time'; assert.equal(f.context.shareableReport(), false);
});

test('report failure allows explicit retry without exposing an earlier preview', async () => {
  const f = fixture(); f.context.api = async () => { throw new Error('Safe unavailable message'); };
  await f.context.generateReport();
  assert.match(f.$('#reportError').textContent, /Safe unavailable/);
  assert.equal(f.$('#previewReport').disabled, false); assert.equal(f.state.report, null);
  f.context.api = async () => report; await f.context.generateReport();
  assert.equal(f.state.report, report);
});

test('safe diagnostics use text nodes and can be cleared for a new check', () => {
  const f = fixture(); f.context.sourceDiagnostic({ diagnostic: { title: '<unsafe>', steps: ['<script>'] } });
  assert.equal(f.$('#sourceDiagnosticTitle').textContent, '<unsafe>');
  assert.equal(f.$('#sourceDiagnosticSteps').children[0].textContent, '<script>');
  f.context.sourceDiagnostic(); assert.equal(f.$('#sourceDiagnostic').hidden, true);
  assert.equal(f.$('#sourceDiagnosticSteps').children.length, 0);
});

test('hosting guidance failure retains a visible retry and discards stale workspace response', async () => {
  const f = fixture(); f.context.api = async () => { throw new Error('Connection lost'); };
  await f.context.loadHostingGuides();
  assert.equal(f.$('#retryHostingGuides').hidden, false);
  let resolve; f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.loadHostingGuides(); f.state.authEpoch++;
  resolve({ profiles: [{ id: 'neon' }], default_profile: 'neon' }); await pending;
  assert.equal(f.state.hostingGuides.length, 0);
});

test('source continuation remains unavailable for unsaved source changes', () => {
  const f = fixture(); f.state.sourceDirty = true;
  f.context.updateSourceContinue({ readiness: { status: 'ready' } });
  assert.equal(f.$('#sourceContinue').hidden, true);
});

test('readable report renders headings and code using text nodes, never active HTML', () => {
  const f = fixture();
  f.context.renderReportDocument('# Report\n\n## Question\n\n&lt;img src=x onerror=alert(1)&gt;\n\n````sql\n```\n<script>\n````\n');
  const children = f.$('#reportDocument').children;
  assert.equal(children[0].tag, 'h2'); assert.equal(children[1].tag, 'h3');
  assert.equal(children[2].tag, 'p'); assert.equal(children[2].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(children[3].tag, 'pre'); assert.equal(children[3].children[0].textContent, '```\n<script>\n');
  assert.equal(children.some(node => node.tag === 'img' || node.tag === 'script'), false);
});

test('failed runs expose retained captures without fabricating a completed assessment', () => {
  const f = fixture(); f.state.runs = [];
  const original = f.context.$;
  f.context.$ = selector => { const node = original(selector); node.dataset ||= {}; node.options ||= []; node.insertAdjacentHTML = () => {}; return node; };
  f.context.viewState = { focusBookmark: () => ({}), currentOperation: () => 'Failed', captureStatus: () => 'Partial evidence' };
  f.context.document = {};
  f.context.sessionStorage = { setItem() {} };
  f.context.terminal = () => true;
  f.context.renderExecutionProgress = () => {};
  f.context.renderHistory = () => {};
  f.context.readiness = () => {};
  vm.runInContext(source.slice(source.indexOf('  function renderRun('), source.indexOf('  function evidenceMarkup(')), f.context);
  f.context.renderRun({ id: 'run-a', source_id: 'source-a', status: 'failed', question: 'A bounded question', evidence: [{ evidence_hash: 'digest', rows: [[1]] }], error: { code: 'model_failed', message: 'Model unavailable' } });
  assert.equal(f.$('#answerThread').hidden, false); assert.equal(f.$('#conclusionCard').hidden, true);
  assert.equal(f.$('#evidenceSection').hidden, false); assert.match(f.$('#limitations').innerHTML, /No completed model assessment/);
  assert.match(f.$('#evidenceLedger').innerHTML, /Partial evidence/);
  assert.equal(f.$('#answerContext').textContent, 'Saved captures · no completed model assessment');
});


test('readable report decodes escaped prose punctuation without creating active markup', () => {
  const f = fixture();
  assert.equal(f.context.reportProse(String.raw`\[value\]\-\_\+\. &lt;script&gt;`), '[value]-_+. <script>');
  f.context.renderReportDocument('**Partial attempt:** Keep the captured reads.\n- First reference\n- Second reference\n\nA saved snapshot.');
  const nodes = f.$('#reportDocument').children;
  assert.equal(nodes[0].className, 'report-warning');
  assert.equal(nodes[0].children[0].tag, 'strong');
  assert.equal(nodes[1].tag, 'ul');
  assert.equal(nodes[1].children.length, 2);
  assert.equal(nodes[2].textContent, 'A saved snapshot.');
});

test('saved terminal connection is shown after metadata loads without approving tables', async () => {
  const f = fixture();
  f.context.api = async () => ({ profiles: [], default_profile: 'supabase', saved_connection: { secret_ref: 'OPSGRAPH_SOURCE_DSN', allowed_schemas: ['public'] } });
  await f.context.loadHostingGuides();
  assert.equal(f.$('#savedConnection').hidden, false);
  assert.equal(f.state.savedConnection.secret_ref, 'OPSGRAPH_SOURCE_DSN');
  f.state.sources = [{ secret_ref: 'OPSGRAPH_SOURCE_DSN' }];
  f.context.renderSavedConnection();
  assert.equal(f.$('#savedConnection').hidden, true);
  f.state.sources = []; f.state.authenticated = false;
  f.context.renderSavedConnection();
  assert.equal(f.$('#savedConnection').hidden, true);
});

test('late saved connection metadata never crosses workspace boundaries', async () => {
  const f = fixture(); let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.loadHostingGuides();
  f.state.authEpoch++;
  resolve({ profiles: [], default_profile: 'supabase', saved_connection: { secret_ref: 'OTHER_SECRET' } });
  await pending;
  assert.equal(f.state.savedConnection, undefined);
});

function discoveryFixture() {
  const f = fixture();
  f.state.savedConnection = { secret_ref: 'OPSGRAPH_SOURCE_DSN' }; f.state.sourceSetupToken = 1;
  f.$('#sourceSecretRef').value = 'OPSGRAPH_SOURCE_DSN';
  f.context.markSourceDirty = () => { f.state.sourceDirty = true; };
  f.context.document = {
    createElement: tag => ({ tag, children: [], append(...items) { this.children.push(...items); }, addEventListener(type, handler) { this[type] = handler; } }),
    createTextNode: text => ({ textContent: text }),
  };
  vm.runInContext(source.slice(source.indexOf('  async function discoverConnectionTables()'), source.indexOf('  function sourceSetup(')), f.context);
  return f;
}

test('discovery lists names without granting access and preserves manual selections', async () => {
  const f = discoveryFixture(); const requests = [];
  f.$('#sourceTables').value = 'public.manual';
  f.context.api = async (path, options) => { requests.push({ path, options }); return { tables: ['public.jobs'], truncated: false }; };
  await f.context.discoverConnectionTables();
  assert.equal(requests.length, 1); assert.equal(requests[0].path, '/api/postgres/discover-tables');
  assert.equal(f.$('#sourceTables').value, 'public.manual');
  const checkbox = f.$('#tableChoices').children[0].children[0];
  assert.equal(checkbox.checked, false);
  checkbox.checked = true; checkbox.change();
  assert.equal(f.$('#sourceTables').value, 'public.manual, public.jobs');
  assert.equal(f.state.sourceDirty, true);
  assert.equal(f.$('#retryTableDiscovery').disabled, false);
});

for (const change of ['workspace', 'form']) test(`late table discovery discarded after ${change} changes`, async () => {
  const f = discoveryFixture(); let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const pending = f.context.discoverConnectionTables();
  if (change === 'workspace') f.state.authEpoch++; else f.state.sourceSetupToken++;
  resolve({ tables: ['public.old'] }); await pending;
  assert.equal(f.$('#tableChoices').children.length, 0);
});

test('empty and failed table discovery allow manual entry and explicit retry', async () => {
  const f = discoveryFixture(); f.context.api = async () => ({ tables: [], truncated: false });
  await f.context.discoverConnectionTables();
  assert.match(f.$('#tableDiscoveryStatus').textContent, /No readable tables/);
  f.context.api = async () => { throw new Error('Connection unavailable'); };
  await f.context.discoverConnectionTables();
  assert.match(f.$('#tableDiscoveryStatus').textContent, /Retry or enter table names/);
  assert.equal(f.$('#retryTableDiscovery').disabled, false);
});

test('conversation replies render once and hide investigation output even if a retained answer exists', () => {
  const f = fixture(); f.state.runs = [];
  const original = f.context.$;
  f.context.$ = selector => { const node = original(selector); node.dataset ||= {}; node.options ||= []; node.insertAdjacentHTML = () => {}; return node; };
  f.context.viewState = { focusBookmark: () => ({}), currentOperation: () => 'Completed', captureStatus: () => 'No query' };
  f.context.document = {};
  f.context.sessionStorage = { setItem() {} };
  f.context.terminal = () => true;
  f.context.renderExecutionProgress = () => {};
  f.context.renderHistory = () => {};
  f.context.readiness = () => {};
  vm.runInContext(source.slice(source.indexOf('  function renderRun('), source.indexOf('  function evidenceMarkup(')), f.context);
  f.context.renderRun({ id: 'run-chat', source_id: 'source-a', status: 'completed', question: 'What can you do?', response_kind: 'conversation', assistant_message: 'I investigate approved PostgreSQL data.', answer: { summary: 'I investigate approved PostgreSQL data.', findings: [], limitations: [] }, evidence: [] });
  assert.equal(f.$('#conversationReply').hidden, false);
  assert.equal(f.$('#conversationReplyText').textContent, 'I investigate approved PostgreSQL data.');
  assert.equal(f.$('#answerThread').hidden, true);
  assert.equal(f.$('#conclusionCard').hidden, true);
  assert.equal(f.$('.execution-card').hidden, true);
  assert.equal(f.$('.run-scope').hidden, true);
  assert.equal(f.$('#openReport').disabled, true);
});

test('automatic table discovery preserves setup focus and handles failure with an enabled retry', async () => {
  const f = discoveryFixture(); let focused = false;
  const original = f.context.$;
  f.context.$ = selector => { const node = original(selector); node.dataset ||= {}; node.reset = () => {}; node.focus = () => { if (selector === '#sourceSetupTitle') focused = true; }; return node; };
  f.context.showView = () => {}; f.context.renderHostingGuide = () => {};
  f.context.sourceDiagnostic = () => {}; f.context.updateSourceContinue = () => {};
  f.context.invalidateRoleGuide = () => {};
  f.context.crypto = { getRandomValues: value => value };
  f.context.api = async () => { throw new Error('Connection unavailable'); };
  vm.runInContext(source.slice(source.indexOf('  function sourceSetup('), source.indexOf('  function markSourceDirty(')), f.context);
  f.context.sourceSetup();
  assert.equal(focused, true);
  assert.equal(f.$('#retryTableDiscovery').disabled, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(f.$('#tableDiscoveryStatus').textContent, /Connection unavailable.*Retry/);
  assert.equal(f.$('#retryTableDiscovery').disabled, false);
  assert.equal(f.$('#sourceSecretRef').value, 'OPSGRAPH_SOURCE_DSN');
});

test('classification disclosure stays expanded during refresh of the same finding only', () => {
  const f = fixture(); f.state.runs = []; f.state.report = null;
  const original = f.context.$;
  f.context.$ = selector => { const node = original(selector); node.dataset ||= {}; node.options ||= []; node.insertAdjacentHTML = () => {}; return node; };
  f.context.viewState = { focusBookmark: () => ({}), currentOperation: () => 'Completed', captureStatus: () => 'Recorded evidence', classificationExplanation: () => 'Model assessment, not independently verified.' };
  f.context.document = {}; f.context.sessionStorage = { setItem() {} }; f.context.terminal = () => true;
  f.context.renderExecutionProgress = () => {}; f.context.renderHistory = () => {}; f.context.readiness = () => {};
  vm.runInContext(source.slice(source.indexOf('  function renderRun('), source.indexOf('  function evidenceMarkup(')), f.context);
  f.$('#findingGrid').querySelectorAll = () => [{ dataset: { focusKey: 'classification:run-a:0' } }];
  const run = { id: 'run-a', source_id: 'source-a', status: 'completed', question: 'Bounded question', answer: { summary: 'Saved answer', findings: [{ claim: 'One captured result', classification: 'supported', evidence_ids: [] }] }, evidence: [] };
  f.context.renderRun(run);
  assert.match(f.$('#findingGrid').innerHTML, /class="classification-detail" open/);
  assert.match(f.$('#findingGrid').innerHTML, /not independently verified/);
  f.context.renderRun({ ...run, id: 'run-b' });
  assert.doesNotMatch(f.$('#findingGrid').innerHTML, /class="classification-detail" open/);
});
