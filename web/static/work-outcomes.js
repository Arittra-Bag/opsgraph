(() => {
  'use strict';
  const labels = { overdue_unresolved: 'Overdue and still not ready', ready: 'Recorded as ready', cancelled: 'Cancelled', terminal_failure: 'Recorded as permanently failed', pending_within_deadline: 'Still within deadline', unknown: 'Cannot determine' };
  // Presentation only: saved answers, calculation inputs and hashes are untouched.
  function readable(value) {
    const limitations = {
      'Outcome definitions and source coverage are operator-reviewed premises, not verified business truth.': 'These results use the rules and data-completeness assumptions you reviewed. OpsGraph cannot verify those assumptions.',
      'Examples are bounded; state totals come from the full selected population.': 'Totals include every export request in your chosen period. The list shows only a limited number of examples.',
      'Ready database records do not prove customer access or an incident cause.': 'Recorded as ready does not prove the customer can open the export or explain what caused a problem.'
    };
    if (Object.prototype.hasOwnProperty.call(limitations, value)) return limitations[value];
    return String(value ?? '').replace(/^(\d+) overdue unresolved exports across (\d+) customers; (\d+) terminal failures and (\d+) unknown among (\d+) accepted promises\.$/, (_, overdue, customers, failed, unknown, total) => `${overdue} exports are overdue and still not ready, affecting ${customers} customers. Of ${total} accepted export requests, ${failed} are recorded as permanently failed and ${unknown} have results we cannot determine.`);
  }
  const terminal = status => ['completed', 'failed', 'blocked', 'cancelled', 'interrupted'].includes(status);
  // datetime-local is explicitly labelled UTC; never interpret it in browser-local time.
  function utc(value) {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) throw new Error('Enter a complete UTC date and time.');
    const normalized = value.length === 16 ? `${value}:00` : value;
    const date = new Date(`${normalized}Z`);
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== normalized) throw new Error('Enter a valid UTC date and time.');
    return date.toISOString();
  }
  function scope(contract, values) {
    if (!contract) throw new Error('Select reviewed export rules first.');
    const result = { contract_id: contract.id, version: contract.version, tenant_id: values.tenant.trim(), start: utc(values.start), end: utc(values.end), as_of: utc(values.cutoff) };
    if (!result.tenant_id) throw new Error('Enter the tenant identifier.');
    if (!(result.start < result.end && result.end <= result.as_of)) throw new Error('The acceptance period must end after it starts. Check results at or after the period ends.');
    if (new Date(result.end) - new Date(result.start) > 31 * 86400000) throw new Error('Use an acceptance window of at most 31 days.');
    return result;
  }
  function sameCohort(a, b) {
    return ['contract_id', 'version', 'tenant_id', 'start', 'end'].every(k => a?.[k] === b?.[k]);
  }
  function comparison(before, after) {
    if (!before || !after || !sameCohort(before.scope, after.scope) || before.scope.as_of >= after.scope.as_of) return '';
    const left = before.facts, right = after.facts;
    if (left.population !== right.population) return 'The number of accepted export requests changed between checks. We cannot compare which exports became ready.';
    const a = left.examples.filter(item => item.state === 'overdue_unresolved');
    const b = new Map(right.examples.map(item => [item.promise_id, item]));
    const recovered = a.filter(item => b.get(item.promise_id)?.state === 'ready').length;
    const complete = left.overdue_examples_complete && a.every(item => b.has(item.promise_id));
    return `Overdue and still not ready: ${left.counts.overdue_unresolved} → ${right.counts.overdue_unresolved}. ${complete ? recovered : `At least ${recovered}`} previously overdue export requests are now recorded as ready${complete ? '' : ' in the limited examples'}. This does not explain why they became ready.`;
  }
  function mount({ request, authenticated, openRun, download, document: doc = document, schedule = setTimeout, unschedule = clearTimeout }) {
    const $ = id => doc.getElementById(id);
    if (!$('mwForm')) return null;
    let generation = 0, connected = false, busy = false, contracts = [], run = null, timer = null, previous = null, history = [];
    const text = (id, value) => { $(id).textContent = value; };
    const error = (value = '') => { text('mwError', value); $('mwError').hidden = !value; };
    const current = token => connected && authenticated() && token === generation;
    const identifier = value => {
      if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(value)) throw new Error('Invalid saved audit identifier.');
      return value;
    };
    function controls() {
      $('mwFields').disabled = !connected || busy || !contracts.length;
      $('mwReviewFields').disabled = !connected || busy;
      $('mwRefresh').disabled = !connected || busy;
      $('mwRecheckFields').disabled = !connected || busy || run?.status !== 'completed' || !run?.answer?.work_outcome;
      $('mwInspect').disabled = !connected || busy || !run;
      $('mwExport').disabled = !connected || busy || !run;
      $('mwCancel').hidden = !run || terminal(run.status);
      $('mwCancel').disabled = !connected || run?.status === 'cancelling';
      $('mwHistory').querySelectorAll('button').forEach(node => { node.disabled = busy; });
    }
    function node(tag, value) { const element = doc.createElement(tag); element.textContent = value; return element; }
    function renderHistory() {
      $('mwHistory').replaceChildren();
      history.forEach(item => {
        const scope = item.answer?.work_outcome?.scope || item.work_outcome;
        const button = node('button', `${item.status} · ${scope.tenant_id} · checked through ${scope.as_of} · ${item.id}`);
        button.type = 'button'; button.className = 'outcome-history secondary';
        button.addEventListener('click', () => void load(item.id)); $('mwHistory').append(button);
      });
      if (!history.length) text('mwHistory', 'No saved export audits yet.');
      controls();
    }
    function definition() {
      const selected = $('mwContract').value === '' ? null : contracts[Number($('mwContract').value)];
      text('mwDefinition', selected ? JSON.stringify(selected.definition, null, 2) : 'No export rules selected.');
    }
    function clearResult() {
      run = null; previous = null; $('mwResult').hidden = true;
      ['mwResultTitle', 'mwScope', 'mwCounts', 'mwQuality', 'mwComparison', 'mwExamples', 'mwSampleNote', 'mwLimitations', 'mwEvidence'].forEach(id => $(id).replaceChildren());
      $('mwLater').value = '';
    }
    function reset() {
      generation++; connected = false; busy = false; unschedule(timer); timer = null; contracts = []; history = [];
      clearResult(); $('mwForm').reset(); $('mwReviewForm').reset(); $('mwContract').replaceChildren(node('option', 'Select reviewed export rules')); $('mwContract').firstChild.value = '';
      text('mwDefinition', 'No export rules selected.'); text('mwHistory', 'No audits loaded.'); text('mwStatus', 'Connect workspace to load reviewed export rules.'); error(); controls();
    }
    async function refresh() {
      if (!connected || busy) return;
      const token = ++generation; unschedule(timer); error(); busy = true; controls();
      text('mwStatus', 'Loading reviewed contracts and saved audits…');
      try {
        const [definitions, audits] = await Promise.all([request('/api/work-outcomes/contracts'), request('/api/runs')]);
        if (!current(token)) return;
        const selected = contracts[Number($('mwContract').value)]?.definition;
        contracts = definitions;
        $('mwContract').replaceChildren(node('option', 'Select reviewed export rules')); $('mwContract').firstChild.value = '';
        contracts.forEach((item, index) => { const option = node('option', `${item.definition.id} · v${item.definition.version} · ${item.definition.source_id}`); option.value = String(index); $('mwContract').append(option); });
        const selectedIndex = contracts.findIndex(item => item.definition.id === selected?.id && item.definition.version === selected?.version && item.definition.source_id === selected?.source_id);
        $('mwContract').value = selectedIndex >= 0 ? String(selectedIndex) : contracts.length === 1 ? '0' : '';
        definition(); history = audits.filter(item => item.work_outcome); renderHistory();
        text('mwStatus', contracts.length ? `${contracts.length} reviewed export rule set(s). PostgreSQL readiness and permissions are checked again for every run.` : 'No reviewed export rules. Review the advanced setup below after connecting a source.');
      } catch (failure) { if (current(token)) error(failure.message); }
      finally { if (current(token)) { busy = false; controls(); } }
    }
    async function connect() { reset(); connected = authenticated(); controls(); if (connected) await refresh(); }
    function render(saved) {
      run = saved; $('mwResult').hidden = false; controls();
      text('mwResultTitle', `Export audit · ${saved.status}`);
      const calc = saved.answer?.work_outcome;
      const capturedScope = calc?.scope || saved.work_outcome;
      text('mwScope', capturedScope ? `Tenant ${capturedScope.tenant_id} · exports accepted from ${capturedScope.start} (included) to ${capturedScope.end} (excluded) · checked through ${capturedScope.as_of} · export rules ${capturedScope.contract_id} v${capturedScope.version}` : 'No scope captured.');
      $('mwCounts').replaceChildren(); $('mwExamples').replaceChildren(); $('mwLimitations').replaceChildren();
      text('mwComparison', ''); text('mwQuality', ''); text('mwSampleNote', '');
      if (calc) {
        for (const [state, label] of Object.entries(labels)) {
          const card = node('div', ''); card.className = `outcome-count outcome-${state}`;
          card.append(node('strong', String(calc.facts.counts[state])), node('span', label)); $('mwCounts').append(card);
        }
        text('mwQuality', `${calc.facts.population} accepted export requests · ${calc.facts.affected_customers} customers with overdue exports still not ready · ${calc.facts.orphan_events} events with no matching export request · ${calc.facts.unassignable_events} events that cannot be linked to an export request. Results we cannot determine are counted separately.`);
        text('mwComparison', comparison(previous, calc));
        const examples = calc.facts.examples.filter(item => item.state === 'overdue_unresolved');
        text('mwSampleNote', `${examples.length} of ${calc.facts.counts.overdue_unresolved} exports overdue and still not ready shown. ${calc.facts.overdue_examples_complete ? 'This overdue list is complete for this saved check.' : 'Only some examples are shown; this is not a complete overdue list.'}`);
        examples.forEach(item => { const row = node('tr', ''); [item.promise_id, item.customer_id, item.due_at ? new Date(item.due_at).toISOString() : 'Unknown'].forEach(value => row.append(node('td', value ?? 'Unknown'))); $('mwExamples').append(row); });
        if (!examples.length) { const cell = node('td', 'No overdue examples retained in this capture.'); cell.colSpan = 3; const row = node('tr', ''); row.append(cell); $('mwExamples').append(row); }
        (saved.answer.limitations || []).forEach(value => $('mwLimitations').append(node('li', readable(value))));
        text('mwEvidence', JSON.stringify({ run_id: saved.id, calculation: calc, captures: saved.evidence }, null, 2));
        // Default is a suggestion; the user explicitly submits the new cutoff.
        if (!$('mwLater').value || new Date(`${$('mwLater').value}Z`).getTime() <= new Date(capturedScope.as_of).getTime()) $('mwLater').value = new Date(new Date(capturedScope.as_of).getTime() + 300000).toISOString().slice(0, 19);
      } else {
        text('mwEvidence', JSON.stringify({ run_id: saved.id, scope: saved.work_outcome, captures: saved.evidence, error: saved.error }, null, 2));
      }
      text('mwStatus', terminal(saved.status) ? `Saved ${saved.status} audit ${saved.id}.` : `Audit ${saved.id} is ${saved.status}. Waiting for its database read…`);
      if (saved.error) error(saved.error.message || saved.error.code);
    }
    async function poll(id, token) {
      if (!current(token)) return;
      try {
        const saved = await request(`/api/runs/${identifier(id)}`);
        if (!current(token)) return;
        render(saved);
        if (terminal(saved.status)) {
          // Use this already-authorized saved response; no second read or stale refresh.
          history = [saved, ...history.filter(item => item.id !== saved.id)];
          busy = false; renderHistory();
        }
        else timer = schedule(() => void poll(id, token), 800);
      } catch (failure) { if (current(token)) { busy = false; controls(); error(`${failure.message} Saved audit ${id} can be reopened using Refresh saved audits.`); } }
    }
    async function load(id) {
      if (busy || !connected) return;
      const token = ++generation; busy = true; unschedule(timer); error(); clearResult(); controls();
      await poll(identifier(id), token);
    }
    async function submit(capturedScope, sourceId, parent = null) {
      const token = ++generation; busy = true; unschedule(timer); error(); controls();
      text('mwStatus', 'Starting the export check…');
      try {
        const created = await request('/api/runs', { method: 'POST', body: JSON.stringify({ question: 'Which approved customer exports missed their deadline?', source_id: sourceId, work_outcome: capturedScope, ...(parent ? { parent_run_id: parent } : {}) }) });
        if (!current(token)) return;
        run = created; controls(); await poll(identifier(created.id), token);
      } catch (failure) { if (current(token)) { busy = false; controls(); error(failure.message); } }
    }
    $('mwForm').addEventListener('submit', event => {
      event.preventDefault(); if (busy || !connected) return;
      try {
        const contract = contracts[Number($('mwContract').value)]?.definition;
        // An empty select must not accidentally select index zero.
        if ($('mwContract').value === '') throw new Error('Select reviewed export rules first.');
        const input = scope(contract, { tenant: $('mwTenant').value, start: $('mwStart').value, end: $('mwEnd').value, cutoff: $('mwCutoff').value });
        clearResult(); void submit(input, contract.source_id);
      } catch (failure) { error(failure.message); }
    });
    $('mwRecheckForm').addEventListener('submit', event => {
      event.preventDefault(); if (busy || !connected || run?.status !== 'completed' || !run.answer?.work_outcome) return;
      try {
        const cutoff = utc($('mwLater').value);
        const original = run.answer.work_outcome;
        if (cutoff <= original.scope.as_of) throw new Error(`Choose a check time after ${original.scope.as_of} (UTC), the time of the saved check.`);
        previous = original; void submit({ ...original.scope, as_of: cutoff }, original.contract.source_id, run.id);
      } catch (failure) { error(failure.message); }
    });
    $('mwReviewForm').addEventListener('submit', async event => {
      event.preventDefault(); if (busy || !connected) return;
      const token = ++generation; busy = true; controls(); error();
      try {
        if (!$('mwConfirm').checked) throw new Error('Explicitly review and confirm the business meanings first.');
        const definition = JSON.parse($('mwContractJson').value);
        await request('/api/work-outcomes/contracts', { method: 'POST', body: JSON.stringify({ definition, confirm_reviewed_meanings: true }) });
        if (!current(token)) return;
        $('mwConfirm').checked = false; busy = false; await refresh();
      } catch (failure) { if (current(token)) error(failure.message); }
      finally { if (current(token)) { busy = false; controls(); } }
    });
    $('mwContractJson').addEventListener('input', () => { $('mwConfirm').checked = false; });
    $('mwContract').addEventListener('change', definition);
    $('mwRefresh').addEventListener('click', () => void refresh());
    $('mwInspect').addEventListener('click', () => { if (!busy && run) void openRun(identifier(run.id)); });
    $('mwExport').addEventListener('click', async () => {
      if (busy || !run) return;
      const token = generation, id = identifier(run.id);
      try { const result = await request(`/api/runs/${id}/export`); if (current(token)) download(`${id}-export-audit.json`, result); }
      catch (failure) { if (current(token)) error(failure.message); }
    });
    $('mwCancel').addEventListener('click', async () => {
      if (!connected || !run || terminal(run.status) || run.status === 'cancelling') return;
      const token = generation, id = identifier(run.id); $('mwCancel').disabled = true;
      try { await request(`/api/runs/${id}/cancel`, { method: 'POST' }); if (current(token)) text('mwStatus', 'Cancellation requested; waiting for the backend to confirm.'); }
      catch (failure) { if (current(token)) { error(failure.message); controls(); } }
    });
    reset(); return { connect, reset, refresh, load };
  }
  window.OpsGraphWorkOutcomes = { mount, utc, scope, comparison, sameCohort, readable };
})();
