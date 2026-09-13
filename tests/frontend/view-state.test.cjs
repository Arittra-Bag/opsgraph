'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { eventLabel, currentOperation, operationEvent, captureStatus, classificationExplanation, stageProgress, previewScope, focusBookmark, focusTarget } = require('../../src/opsgraph/web/static/view-state.js');

test('operation labels distinguish actual stage start from completion', () => {
  assert.equal(eventLabel({ type: 'stage_started', data: { stage: 'plan' } }), 'Planning bounded PostgreSQL queries');
  assert.equal(eventLabel({ type: 'stage_completed', data: { stage: 'plan' } }), 'Query planning completed');
  assert.equal(eventLabel({ type: 'query_started', data: { purpose: 'Read failed job records' } }), 'Query started · Read failed job records');
  assert.equal(eventLabel({ type: 'answer_validation_retry', data: { stage: 'reconcile', reason: 'private' } }), 'Rechecking the answer against captured evidence · reconcile');
});

test('query plan correction is activity before execution', () => {
  assert.equal(eventLabel({ type: 'plan_validation_retry', data: { reason: 'empty_parent_join_conflict' } }), 'Rechecking the query plan before execution');
});

test('terminal state overrides replayed earlier stages; another run never supplies progress', () => {
  const event = { run_id: 'one', type: 'stage_started', data: { stage: 'execute' } };
  assert.match(currentOperation({ id: 'one', status: 'completed' }, event), /^Completed/);
  assert.match(currentOperation({ id: 'two', status: 'running' }, event), /waiting for recorded/);
  assert.match(currentOperation({ id: 'one', status: 'cancelling' }, event), /waiting for the current operation to exit/);
});

test('a schema check remains activity without replacing an active model operation', () => {
  const planning = { run_id: 'one', type: 'stage_started', data: { stage: 'plan' } };
  const checked = { run_id: 'one', type: 'schema_checked', data: { checked_at: '2026-09-12T00:00:00Z' } };
  assert.equal(eventLabel(checked), 'Database schema checked');
  assert.equal(operationEvent(planning, checked), planning);
  assert.match(currentOperation({ id: 'one', status: 'running' }, operationEvent(planning, checked)), /Planning bounded/);
  assert.equal(operationEvent(null, planning), planning);
});

test('clarification is actionable without claiming completion or captured evidence', () => {
  const run = { status: 'blocked', error: { code: 'clarification_required' }, evidence: [] };
  assert.match(currentOperation(run), /answer the question below in a follow-up/);
  assert.match(captureStatus(run), /^No captured database evidence/);
});

test('failed, running and cancelled runs keep partial capture labels', () => {
  for (const status of ['failed', 'blocked', 'running', 'interrupted', 'cancelled', 'cancelling']) {
    const description = captureStatus({ status, evidence: [{}, {}] });
    assert.match(description, /^Partial evidence · 2 captures retained/);
    assert.match(description, /no completed answer/);
    assert.match(description, /saved reads, not a live view/);
  }
  assert.match(captureStatus({ status: 'completed', evidence: [{}] }), /^Recorded evidence · 1 capture retained/);
  assert.match(captureStatus({ status: 'completed', evidence: [{}] }), /does not establish complete source coverage/);
});

test('classification explanation does not convert citation membership into verified support', () => {
  assert.match(classificationExplanation('supported'), /^The model classifies/);
  assert.match(classificationExplanation('possible'), /does not establish/);
  assert.match(classificationExplanation('contradictory'), /refuting the proposition/);
  assert.match(classificationExplanation('unknown'), /insufficient evidence/);
  assert.match(classificationExplanation('invalid'), /unavailable.*uncertain/);
});

test('stage progress counts only recorded completions for the selected run', () => {
  const events = [
    { run_id: 'run-1', type: 'stage_started', data: { stage: 'route' }, created_at: '2026-09-13T00:00:00Z' },
    { run_id: 'run-1', type: 'stage_completed', data: { stage: 'route' }, created_at: '2026-09-13T00:00:01Z' },
    { run_id: 'run-2', type: 'stage_completed', data: { stage: 'plan' }, created_at: '2026-09-13T00:00:02Z' },
    { run_id: 'run-1', type: 'stage_started', data: { stage: 'plan' }, created_at: '2026-09-13T00:00:03Z' },
    { run_id: 'run-1', type: 'completed', data: {}, created_at: '2026-09-13T00:00:04Z' },
  ];
  const progress = stageProgress('run-1', events);
  assert.equal(progress.completed, 1);
  assert.equal(progress.total, 4);
  assert.equal(progress.current.id, 'plan');
  assert.equal(progress.durationMs, null);
  assert.deepEqual(progress.stages.map(stage => stage.status), ['complete', 'running', 'pending', 'pending']);
});

test('stage progress derives duration only after four explicit recorded completions', () => {
  const events = ['route', 'plan', 'execute', 'reconcile'].flatMap((stage, index) => [
    { run_id: 'run-1', type: 'stage_started', data: { stage }, created_at: `2026-09-13T00:00:0${index * 2}Z` },
    { run_id: 'run-1', type: 'stage_completed', data: { stage }, created_at: `2026-09-13T00:00:0${index * 2 + 1}Z` },
  ]);
  events.push({ ...events[1], created_at: '2026-09-13T00:00:09Z' });
  const progress = stageProgress('run-1', events);
  assert.equal(progress.completed, 4);
  assert.equal(progress.current, null);
  assert.equal(progress.durationMs, 7000);
  assert.deepEqual(progress.stages.map(stage => stage.elapsedMs), [1000, 3000, 5000, 7000]);
});

test('missing, malformed and unknown stage events never fabricate progress', () => {
  const progress = stageProgress('run-1', [
    { run_id: 'run-1', type: 'stage_completed', data: { stage: 'unknown' }, created_at: '2026-09-13T00:00:00Z' },
    { run_id: 'run-1', type: 'stage_started', data: { stage: 'route' }, created_at: 'invalid' },
    null,
  ]);
  assert.equal(progress.completed, 0);
  assert.equal(progress.current.id, 'route');
  assert.equal(progress.durationMs, null);
  assert.equal(progress.stages[0].elapsedMs, null);
});

const source = { allowed_schemas: ['public', 'jobs'], allowed_tables: ['public.jobs', 'jobs.archive'] };
const policy = { obligations: { max_rows: 100, timeout_ms: 5000, allowed_schemas: ['public', 'jobs'], allowed_tables: [] } };
const skill = settings => ({ tools: [{ tool: 'core.sql.select', enabled: true, settings }] });

test('scope preview narrows tables, schemas and numeric bounds', () => {
  assert.deepEqual(previewScope(source, skill({ allowed_schemas: ['jobs'], max_rows: 20, timeout_ms: 2000 }), policy), { tables: ['jobs.archive'], max_rows: 20, timeout_ms: 2000, blocked: '' });
});

test('empty and disjoint playbook allowlists never become unrestricted scope', () => {
  for (const settings of [{ allowed_tables: [] }, { allowed_tables: ['public.other'] }, { allowed_schemas: [] }]) {
    const preview = previewScope(source, skill(settings), policy);
    assert.deepEqual(preview.tables, []);
    assert.match(preview.blocked, /no permitted table scope/);
  }
});

test('scope preview omits unknown bounds rather than inventing server defaults', () => {
  assert.equal(previewScope(source, skill({}), null), null);
  assert.equal(previewScope(null, skill({}), policy), null);
  assert.equal(previewScope(source, null, policy), null);
  assert.equal(previewScope(source, skill({}), { obligations: {} }), null);
  assert.deepEqual(previewScope(source, skill({}), { obligations: { allowed_schemas: ['public', 'jobs'], allowed_tables: [] } }), { tables: ['public.jobs', 'jobs.archive'], max_rows: null, timeout_ms: null, blocked: '' });
});

test('deployment schema ceiling applies even with an unrestricted deployment table list', () => {
  const restricted = { obligations: { ...policy.obligations, allowed_schemas: ['public'] } };
  assert.deepEqual(previewScope(source, skill({}), restricted).tables, ['public.jobs']);
  const denied = previewScope(source, skill({}), { obligations: { ...policy.obligations, allowed_schemas: [] } });
  assert.deepEqual(denied.tables, []);
  assert.match(denied.blocked, /deployment policy/);
});

test('deployment bare table names match only inside permitted schemas', () => {
  const repeatedNames = { allowed_schemas: ['public', 'private'], allowed_tables: ['public.records', 'private.records', 'public.other'] };
  const restricted = { obligations: { ...policy.obligations, allowed_schemas: ['public'], allowed_tables: ['records'] } };
  assert.deepEqual(previewScope(repeatedNames, skill({}), restricted).tables, ['public.records']);
  assert.deepEqual(previewScope(repeatedNames, skill({}), { obligations: { ...restricted.obligations, allowed_tables: ['private.records'] } }).tables, []);
});

test('qualified policy table names require an exact match and intersect the playbook', () => {
  const restricted = { obligations: { ...policy.obligations, allowed_tables: ['jobs.archive'] } };
  assert.deepEqual(previewScope(source, skill({}), restricted).tables, ['jobs.archive']);
  assert.deepEqual(previewScope(source, skill({ allowed_tables: ['public.jobs'] }), restricted).tables, []);
  assert.deepEqual(previewScope({ ...source, allowed_tables: [] }, skill({}), policy).tables, []);
});

test('row and timeout previews honor the strictest execution, policy and playbook bounds', () => {
  const restricted = { obligations: { ...policy.obligations, max_rows: 12, timeout_ms: 700 } };
  const preview = previewScope(source, skill({ max_rows: 20, timeout_ms: 500 }), restricted);
  assert.equal(preview.max_rows, 12);
  assert.equal(preview.timeout_ms, 500);
  const broader = previewScope(source, skill({ max_rows: 250, timeout_ms: 15000 }), { obligations: { ...policy.obligations, max_rows: 500, timeout_ms: 30000 } });
  assert.equal(broader.max_rows, 100);
  assert.equal(broader.timeout_ms, 5000);
});

test('a disabled or absent SQL tool is shown as blocked', () => {
  assert.match(previewScope(source, { tools: [] }, policy).blocked, /no enabled SQL query tool/);
  assert.match(previewScope(source, { tools: [{ tool: 'core.sql.select', enabled: false }] }, policy).blocked, /no enabled SQL query tool/);
});

test('focus bookmarks find the same stable history item after replacement', () => {
  const old = { isConnected: true, dataset: { focusKey: 'history:run-1' } };
  const replacement = { isConnected: true, dataset: { focusKey: 'history:run-1' } };
  const unrelated = { isConnected: true, dataset: { focusKey: 'history:run-2' } };
  const document = { activeElement: old, querySelectorAll: () => [unrelated, replacement] };
  const bookmark = focusBookmark(document);
  assert.equal(focusTarget(bookmark, document), old);
  old.isConnected = false;
  assert.equal(focusTarget(bookmark, document), replacement);
});

test('drawer return target survives replaced captures without selecting unrelated content', () => {
  const old = { isConnected: false, dataset: { focusKey: 'capture:run-1:capture-1' } };
  const bookmark = { node: old, key: old.dataset.focusKey };
  assert.equal(focusTarget(bookmark, { querySelectorAll: () => [{ dataset: { focusKey: 'capture:run-2:capture-1' } }] }), null);
  const replacement = { dataset: { focusKey: old.dataset.focusKey } };
  assert.equal(focusTarget(bookmark, { querySelectorAll: () => [replacement] }), replacement);
});
