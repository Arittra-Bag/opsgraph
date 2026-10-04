'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');
function fixture() {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', hidden: false, innerHTML: '', textContent: '', querySelectorAll: () => [] });
    return nodes.get(id);
  };
  const state = { authenticated: true, authEpoch: 1, streamToken: 4, runs: [], conversations: [], run: { id: 'inv-2', conversation_id: 'case-1' } };
  const calls = [];
  const context = vm.createContext({ $, state, api: async path => { calls.push(path); return []; }, esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'), stamp: value => value, preserveFocus: fn => fn(), runId: value => value });
  vm.runInContext(source.slice(source.indexOf('  async function loadHistory('), source.indexOf('  async function loadPolicy(')), context);
  vm.runInContext(source.slice(source.indexOf('  function renderConversationTitle('), source.indexOf('  function evidenceMarkup(')), context);
  return { $, state, context, calls };
}
test('sidebar lists conversations once, with turns and latest selectable run', () => {
  const f = fixture();
  f.state.conversations = [{ id: 'case-1', title: 'Payment failures', source_id: 'test', latest_run_id: 'inv-2', turn_count: 3, status: 'completed', updated_at: 'today' }];
  f.state.runs = [{ id: 'inv-1' }, { id: 'inv-2' }, { id: 'inv-3' }];
  f.context.renderHistory();
  assert.equal((f.$('#caseList').innerHTML.match(/class="case-card/g) || []).length, 1);
  assert.match(f.$('#caseList').innerHTML, /data-run-id="inv-2"/);
  assert.match(f.$('#caseList').innerHTML, /3 turns/);
  assert.equal(f.$('#historyMessage').textContent, '1 investigation');
});
test('history search and empty results are honest and escape remote titles', () => {
  const f = fixture();
  f.state.conversations = [{ id: 'case-1', title: '<script>bad</script>', source_id: 'test', latest_run_id: 'inv-2', status: 'completed' }];
  f.context.renderHistory(); assert.doesNotMatch(f.$('#caseList').innerHTML, /<script>/);
  f.$('#caseSearch').value = 'missing'; f.context.renderHistory();
  assert.equal(f.$('#historyMessage').textContent, 'No matching investigations.');
});
test('history responses from a previous workspace cannot replace current state', async () => {
  const f = fixture(); const pending = [];
  f.context.api = () => new Promise(resolve => pending.push(resolve));
  const load = f.context.loadHistory(); f.state.authEpoch++;
  pending[0]([{ id: 'stale' }]); pending[1]([{ id: 'stale-case' }]); await load;
  assert.equal(f.state.runs.length, 0); assert.equal(f.state.conversations.length, 0);
});
test('conversation timeline retains earlier replies without fabricating evidence for chat', () => {
  const f = fixture();
  f.state.conversation = { id: 'case-1', turns: [{ id: 'inv-1', question: 'What can you do?', response_kind: 'conversation', assistant_message: '<img src=x onerror=bad>Safe explanation', created_at: 'today' }, f.state.run] };
  f.context.renderConversation(); const html = f.$('#previousTurn').innerHTML;
  assert.equal(f.$('#previousTurn').hidden, false);
  assert.match(html, /What can you do/); assert.match(html, /Conversation · no database query/);
  assert.match(html, /&lt;img/); assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /captures/);
});
test('execution turns retain immutable capture counts and an accessible inspection control', () => {
  const f = fixture();
  f.state.conversation = { id: 'case-1', turns: [{ id: 'inv-1', question: 'Payments', status: 'completed', answer: { summary: '50 failures', findings: [{ claim: '<script>bad</script>', classification: 'supported' }] }, evidence: [{ rows: [[50]] }] }, f.state.run] };
  f.context.renderConversation(); const html = f.$('#previousTurn').innerHTML;
  assert.match(html, /1 captures/); assert.match(html, /Inspect saved turn/);
  assert.match(html, /data-run-id="inv-1"/); assert.doesNotMatch(html, /<script>/);
});
test('a stale conversation load cannot replace the newly selected investigation', async () => {
  const f = fixture(); let resolve;
  f.context.api = () => new Promise(done => { resolve = done; });
  const load = f.context.loadConversation(f.state.run, 4); f.state.streamToken = 5;
  resolve({ id: 'case-1', turns: [{ id: 'old' }] }); await load;
  assert.equal(f.state.conversation, undefined);
});
test('timeline from another conversation is never displayed under the selected question', () => {
  const f = fixture(); f.state.conversation = { id: 'other', turns: [{ id: 'secret', question: 'Other history' }] };
  f.context.renderConversation(); assert.equal(f.$('#previousTurn').hidden, true); assert.equal(f.$('#previousTurn').innerHTML, '');
});
test('ordinary capability messages can be submitted without claiming model readiness', () => {
  const f = fixture();
  const code = source.slice(source.indexOf('  function selectedInvestigationSource('), source.indexOf('  function scopeMarkup('));
  f.context.sourceReady = item => item?.status === 'ready';
  f.context.sourceReadinessPassed = item => item?.readiness?.status === 'ready';
  f.context.terminal = () => true; f.context.renderComposerScope = () => {};
  f.state.sources = [{ id: 'test', kind: 'postgresql', status: 'ready', readiness: { status: 'pending' } }];
  f.state.run = null; f.state.providerDirty = true; f.state.modelTested = false;
  const original = f.context.$;
  f.context.$ = id => { const node = original(id); node.classList = { toggle() {} }; node.setAttribute = () => {}; node.removeAttribute = () => {}; return node; };
  vm.runInContext(code, f.context);
  f.$('#investigationSource').value = 'test';
  for (const question of ['Hello!', 'what can you do?', 'So what can you do?', ' THANK YOU. ', 'how does opsgraph work']) {
    f.$('#investigationQuestion').value = question; f.context.readiness(); assert.equal(f.$('#submitRun').disabled, false);
  }
  for (const question of ['What can you do? Drop the database.', 'count payments', 'explain this evidence']) {
    f.$('#investigationQuestion').value = question; f.context.readiness(); assert.equal(f.$('#submitRun').disabled, true);
  }
  f.$('#investigationQuestion').value = 'hello'; f.state.sourceDirty = true; f.state.sourceEditingId = 'test';
  f.context.readiness();
  assert.equal(f.$('#submitRun').disabled, true);
  assert.equal(f.$('#modelStepTitle').textContent.includes('complete'), false);
});
test('retries stay collapsed inside a turn and preserve their inspection links', () => {
  const f = fixture(); f.state.run.turn_id = 'turn-1';
  f.state.conversation = { id: 'case-1', turns: [{ id: 'inv-2', turn_id: 'turn-1', attempts: [{ id: 'inv-1', status: 'failed', error: { message: 'Model unavailable' }, evidence: [] }, f.state.run] }] };
  f.context.renderConversation(); const html = f.$('#previousTurn').innerHTML;
  assert.equal(f.$('#previousTurn').hidden, false);
  assert.match(html, /Other attempts in this turn \(1\)/);
  assert.match(html, /data-run-id="inv-1"/);
  assert.doesNotMatch(html, /<details[^>]* open/);
});
test('filtered history reports the visible count against all conversations', () => {
  const f = fixture();
  f.state.conversations = [{ id: 'a', title: 'Payments', source_id: 'test' }, { id: 'b', title: 'Orders', source_id: 'test' }];
  f.$('#caseSearch').value = 'payment'; f.context.renderHistory();
  assert.equal(f.$('#historyMessage').textContent, '1 of 2 investigations');
});
test('conversation header uses its original human title and total turn count', () => {
  const f = fixture(); f.state.run.question = 'Follow-up question';
  f.state.conversation = { id: 'case-1', title: 'Why are payments failing?', turns: [{ id: 'inv-1' }, f.state.run], turn_count: 2 };
  f.context.renderConversationTitle();
  assert.equal(f.$('#runIdentity').textContent, 'Why are payments failing? · 2 turns');
});
test('historical turn inspection submits the follow-up against the latest conversation turn', async () => {
  const f = fixture();
  f.state.run = { id: 'inv-old', conversation_id: 'case-1' };
  f.state.conversation = { id: 'case-1', turns: [{ id: 'inv-old' }, { id: 'inv-latest' }] };
  f.state.modelTested = true; f.state.providerDirty = false;
  f.$('#investigationQuestion').value = 'Count failed payments'; f.$('#investigationSource').value = 'source-a';
  f.context.guardAsyncFocus = () => () => {}; f.context.notice = () => {}; f.context.readiness = () => {};
  f.context.loadProvider = async () => {}; f.context.openRun = async () => {};
  f.context.crypto = { randomUUID: () => 'request-1' };
  let body;
  f.context.api = async (path, options) => { body = JSON.parse(options.body); return { id: 'inv-new' }; };
  vm.runInContext(source.slice(source.indexOf('  async function submitRun('), source.indexOf('  async function cancelRun(')), f.context);
  await f.context.submitRun({ preventDefault() {} });
  assert.equal(body.parent_run_id, 'inv-latest');
  assert.equal(body.conversation_id, 'case-1');
});
test('retry preserves original turn timestamp and exposes every prior conversational attempt', () => {
  const f = fixture();
  f.state.conversation = { id: 'case-1', turns: [{ id: 'inv-retry', turn_id: 'turn-original', turn_created_at: 'original-time', created_at: 'retry-time', question: 'What can you do?', response_kind: 'conversation', assistant_message: 'Current reply', attempts: [{ id: 'inv-original', created_at: 'original-time', status: 'completed', response_kind: 'conversation', assistant_message: '<script>old</script>' }, { id: 'inv-retry', created_at: 'retry-time', status: 'completed', assistant_message: 'Current reply' }] }, f.state.run] };
  f.context.renderConversation(); const html = f.$('#previousTurn').innerHTML;
  assert.match(html, /<small>original-time<\/small>/);
  assert.match(html, /Attempts in this turn \(2\)/);
  assert.match(html, /data-run-id="inv-original"/);
  assert.match(html, /data-run-id="inv-retry"/);
  assert.doesNotMatch(html, /<details[^>]* open/);
  assert.doesNotMatch(html, /<script>/);
  assert.equal((html.match(/What can you do\?/g) || []).length, 1);
});
