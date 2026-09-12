/* Presentation derived from recorded state. No inference, timers or synthetic results. */
(function (root) {
  'use strict';
  const stages = {
    route: ['Checking scope and playbook', 'Scope and playbook checked'],
    plan: ['Planning bounded PostgreSQL queries', 'Query planning completed'],
    execute: ['Collecting PostgreSQL evidence', 'Planned query execution completed'],
    reconcile: ['Reviewing captured evidence with the model', 'Model evidence review completed'],
  };
  const stageOrder = Object.keys(stages);
  const eventLabels = {
    legacy_imported: 'Previous investigation imported; execution time may be unavailable',
    configured: 'Run configuration recorded', plan_completed: 'Query plan recorded', schema_checked: 'Database schema checked',
    queued: 'Investigation queued', started: 'Execution started', query_started: 'Query started',
    answer_validation_retry: 'Rechecking the answer against captured evidence',
    plan_validation_retry: 'Rechecking the query plan before execution',
    evidence_captured: 'Evidence captured', completed: 'Investigation completed',
    failed: 'Investigation failed', blocked: 'Investigation blocked', interrupted: 'Execution interrupted',
    cancelling: 'Cancellation requested', cancelled: 'Cancellation confirmed',
  };
  function eventLabel(event) {
    const detail = event.data || {};
    if (stages[detail.stage] && ['stage_started', 'stage_completed'].includes(event.type)) {
      return stages[detail.stage][event.type === 'stage_completed' ? 1 : 0];
    }
    return [eventLabels[event.type] || event.type, detail.purpose || detail.stage || ''].filter(Boolean).join(' · ');
  }
  function currentOperation(run, event) {
    const statuses = {
      queued: 'Queued — waiting for the coordinator to start execution.',
      cancelling: 'Cancellation requested — waiting for the current operation to exit.',
      completed: 'Completed — saved results and evidence are available for inspection.',
      failed: 'Failed — review the error and any evidence captured before failure.',
      blocked: run.error?.code === 'clarification_required' ? 'Clarification needed — answer the question below in a follow-up.' : 'Blocked — review the required configuration or permission change.',
      interrupted: 'Interrupted — preserved evidence is available; retry explicitly to collect fresh evidence.',
      cancelled: 'Cancelled — execution stopped; any earlier captures remain available.',
    };
    if (statuses[run.status]) return statuses[run.status];
    return event?.run_id === run.id ? `Latest recorded operation: ${eventLabel(event)}.` : 'Running — waiting for recorded operation details.';
  }
  function operationEvent(previous, event) {
    // A metadata comparison is recorded inside a stage, not a replacement for that stage.
    return event.type === 'schema_checked' ? previous : event;
  }
  function captureStatus(run) {
    const count = (run.evidence || []).length;
    if (!count) return 'No captured database evidence is available for this attempt.';
    const label = run.status === 'completed' ? 'Recorded evidence' : 'Partial evidence';
    return `${label} · ${count} capture${count === 1 ? '' : 's'} retained. ${run.status === 'completed' ? 'A completed investigation does not establish complete source coverage.' : 'This attempt has no completed answer; earlier captures remain inspectable.'} Collection times below describe saved reads, not a live view of the source.`;
  }
  function classificationExplanation(value) {
    return {
      supported: 'The model classifies this as a direct observation in the cited evidence.',
      possible: 'The model classifies this as an interpretation that the cited evidence does not establish.',
      unknown: 'The model reports insufficient evidence to establish this claim.',
      contradictory: 'The model classifies the cited evidence as refuting the proposition in this finding.',
    }[value] || 'The classification is unavailable; treat this claim as uncertain.';
  }
  function stageProgress(runId, events) {
    const recorded = new Map(stageOrder.map(stage => [stage, { status: 'pending', startedAt: null, completedAt: null }]));
    for (const event of events || []) {
      if (event?.run_id !== runId || !['stage_started', 'stage_completed'].includes(event.type)) continue;
      const stage = event.data?.stage;
      if (!recorded.has(stage)) continue;
      const value = recorded.get(stage);
      const timestamp = Date.parse(event.created_at || '');
      if (event.type === 'stage_started') {
        if (value.status !== 'complete') value.status = 'running';
        if (value.startedAt == null && Number.isFinite(timestamp)) value.startedAt = timestamp;
      } else {
        value.status = 'complete';
        if (value.completedAt == null && Number.isFinite(timestamp)) value.completedAt = timestamp;
      }
    }
    const starts = [...recorded.values()].map(value => value.startedAt).filter(Number.isFinite);
    const firstStartedAt = starts.length ? Math.min(...starts) : null;
    const rows = stageOrder.map(stage => {
      const value = recorded.get(stage);
      const timestamp = value.status === 'complete' ? value.completedAt : value.startedAt;
      return {
        id: stage,
        label: stages[stage][value.status === 'complete' ? 1 : 0],
        status: value.status,
        elapsedMs: firstStartedAt != null && timestamp != null ? Math.max(0, timestamp - firstStartedAt) : null,
      };
    });
    const completed = rows.filter(row => row.status === 'complete').length;
    const completedTimes = [...recorded.values()].map(value => value.completedAt).filter(Number.isFinite);
    return {
      completed,
      total: stageOrder.length,
      current: rows.find(row => row.status === 'running') || null,
      durationMs: completed === stageOrder.length && firstStartedAt != null && completedTimes.length
        ? Math.max(0, Math.max(...completedTimes) - firstStartedAt)
        : null,
      stages: rows,
    };
  }
  function previewScope(source, skill, policy) {
    if (!source || !skill || !policy?.obligations) return null;
    const ceiling = policy.obligations;
    if (!Array.isArray(ceiling.allowed_schemas) || !Array.isArray(ceiling.allowed_tables)) return null;
    const binding = (skill.tools || []).find(item => item.tool === 'core.sql.select' && item.enabled !== false);
    if (!binding) return { tables: [], max_rows: null, timeout_ms: null, blocked: 'The selected playbook has no enabled SQL query tool.' };
    const settings = binding.settings || {};
    // An empty deployment table ceiling means any table in its permitted schemas.
    // Explicit empty source or playbook allowlists still deny; they never inherit.
    const tables = (source.allowed_tables || []).filter(table =>
      (!ceiling.allowed_tables.length || ceiling.allowed_tables.includes(table) || ceiling.allowed_tables.includes(table.split('.').at(-1))) &&
      (settings.allowed_tables == null || settings.allowed_tables.includes(table)));
    const schemas = new Set((source.allowed_schemas || []).filter(schema => ceiling.allowed_schemas.includes(schema) && (settings.allowed_schemas == null || settings.allowed_schemas.includes(schema))));
    const permitted = tables.filter(table => schemas.has(table.split('.')[0]));
    // Current execution ceilings; the backend records authoritative per-run bounds.
    const minimum = (executionCeiling, policyCeiling, limit) => policyCeiling == null ? null : Math.min(executionCeiling, policyCeiling, limit ?? executionCeiling);
    return { tables: permitted, max_rows: minimum(100, ceiling.max_rows, settings.max_rows), timeout_ms: minimum(5000, ceiling.timeout_ms, settings.timeout_ms), blocked: permitted.length ? '' : 'Source, deployment policy and playbook have no permitted table scope. Adjust the source or select another playbook within deployment policy.' };
  }
  function focusBookmark(document) {
    const node = document.activeElement;
    return { node, key: node?.dataset?.focusKey || null };
  }
  function focusTarget(bookmark, document) {
    if (bookmark?.node?.isConnected) return bookmark.node;
    return bookmark?.key ? [...document.querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === bookmark.key) || null : null;
  }
  const api = { eventLabel, currentOperation, operationEvent, captureStatus, classificationExplanation, stageProgress, previewScope, focusBookmark, focusTarget };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OpsGraphViewState = api;
})(typeof window !== 'undefined' ? window : globalThis);
