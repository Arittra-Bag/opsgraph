'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const viewState = require('../../src/opsgraph/web/static/view-state.js');
const source = fs.readFileSync('src/opsgraph/web/static/app.js', 'utf8');

function fixture(width = 1280) {
  const nodes = new Map();
  const document = { activeElement: null };
  const $ = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      hidden: false, isConnected: true, attributes: {}, dataset: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      focus() { document.activeElement = this; },
      scrollIntoView(options) { this.scrolled = options; },
    });
    return nodes.get(selector);
  };
  const state = { authEpoch: 0, authenticated: false, activeDrawer: null };
  const hidden = [];
  const context = vm.createContext({ $, $$: () => [], state, document, viewState,
    window: { matchMedia: () => ({ matches: width <= 900 }) },
    setHistoryHidden(value) { hidden.push(value); $('#historyPanel').hidden = value; },
  });
  vm.runInContext(source.slice(source.indexOf('  function showView('), source.indexOf('  function openDrawer(')), context);
  vm.runInContext(source.slice(source.indexOf('  function positionSelectedTurn('), source.indexOf('  async function openRun(')), context);
  return { $, state, document, context, hidden };
}

test('returning to investigations focuses the visible question when history is hidden', () => {
  const f = fixture(); f.$('#historyPanel').hidden = true;
  f.context.showView('investigations');
  assert.equal(f.document.activeElement, f.$('#caseTitle'));
  assert.equal(f.$('#caseTitle').attributes.tabindex, '-1');
});

test('new investigation focuses its composer heading rather than the hidden history heading', () => {
  const f = fixture(); f.$('#historyPanel').hidden = true; f.$('#runWorkspace').hidden = true;
  f.context.showView('investigations');
  assert.equal(f.document.activeElement, f.$('#composerTitle'));
});

test('visible history remains the investigation navigation heading', () => {
  const f = fixture(); f.context.showView('investigations');
  assert.equal(f.document.activeElement, f.$('[data-view-panel="investigations"] h1'));
});

for (const width of [390, 768, 900, 901, 1280]) test(`selecting a turn at ${width}px focuses it and collapses only narrow history`, () => {
  const f = fixture(width); f.context.positionSelectedTurn();
  assert.deepEqual(f.hidden, width <= 900 ? [true] : []);
  assert.equal(f.document.activeElement, f.$('#caseTitle'));
  assert.equal(f.$('.operator-message').scrolled.block, 'start');
  assert.equal(f.$('.operator-message').scrolled.behavior, 'instant');
});

test('late turn positioning does not move focus behind a drawer or in another view', () => {
  for (const condition of ['drawer', 'view']) {
    const f = fixture(390); const other = f.$('#other'); other.focus();
    if (condition === 'drawer') f.state.activeDrawer = {};
    else f.$('[data-view-panel="investigations"]').hidden = true;
    f.context.positionSelectedTurn();
    assert.equal(f.document.activeElement, other); assert.equal(f.hidden.length, 0);
  }
});

test('conversation refresh restores focused disclosure summaries and retains expanded retry history', () => {
  const f = fixture(); let controls = [];
  f.document.querySelectorAll = () => controls;
  const panel = f.$('#previousTurn');
  const old = { isConnected: true, dataset: { focusKey: 'turn-summary:old' } };
  f.document.activeElement = old;
  panel.querySelectorAll = () => [{ dataset: { attempt: 'old' } }, { dataset: { attempt: 'other-attempts:turn-new' } }];
  Object.defineProperty(panel, 'innerHTML', { set(value) {
    this.html = value; old.isConnected = false; f.document.activeElement = null;
    controls = [...value.matchAll(/data-focus-key="([^"]+)"/g)].map(match => ({
      isConnected: true, dataset: { focusKey: match[1] }, focus() { f.document.activeElement = this; },
    }));
  } });
  f.state.run = { id: 'new', conversation_id: 'case', turn_id: 'turn-new' };
  f.state.conversation = { id: 'case', turns: [
    { id: 'old', status: 'completed', question: 'First question', evidence: [] },
    { ...f.state.run, attempts: [{ id: 'failed', status: 'failed' }, f.state.run] },
  ] };
  f.context.esc = value => String(value ?? ''); f.context.stamp = value => value;
  vm.runInContext(source.slice(source.indexOf('  function preserveFocus('), source.indexOf('  function guardAsyncFocus(')), f.context);
  vm.runInContext(source.slice(source.indexOf('  function historicalAttempts('), source.indexOf('  async function loadConversation(')), f.context);
  f.context.renderConversation();
  assert.equal(f.document.activeElement.dataset.focusKey, 'turn-summary:old');
  assert.match(panel.html, /data-attempt="old" open/);
  assert.match(panel.html, /data-attempt="other-attempts:turn-new" open/);
  assert.match(panel.html, /data-focus-key="other-attempt-summary:turn-new"/);
  assert.match(panel.html, /data-focus-key="attempt:failed"/);
});

for (const moved of [false, true]) test(`delayed conversation selection ${moved ? 'respects moved focus' : 'positions its selected turn'}`, async () => {
  const f = fixture(); f.document.body = {};
  const historyCard = f.$('#historyCard'); historyCard.focus();
  f.context.stopStream = () => { f.state.streamToken = (f.state.streamToken || 0) + 1; };
  f.context.notice = () => {}; f.context.runId = id => id;
  f.context.api = async () => ({ id: 'run-selected' });
  f.context.renderRun = run => { f.state.run = run; };
  f.context.showView = () => {}; f.context.streamRun = () => {};
  f.$('#activityLog').replaceChildren = () => {};
  let resolveConversation; let started;
  const conversationStarted = new Promise(resolve => { started = resolve; });
  f.context.loadConversation = () => { started(); return new Promise(resolve => { resolveConversation = resolve; }); };
  vm.runInContext(source.slice(source.indexOf('  async function openRun('), source.indexOf('  function addEvent(')), f.context);
  const opening = f.context.openRun('run-selected');
  await conversationStarted;
  if (moved) f.$('#investigationQuestion').focus();
  resolveConversation(); await opening;
  assert.equal(f.document.activeElement, f.$(moved ? '#investigationQuestion' : '#caseTitle'));
  assert.equal(Boolean(f.$('.operator-message').scrolled), !moved);
});
