(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const json = value => JSON.stringify(value, null, 2);
  const stamp = value => value ? new Date(value).toLocaleString(undefined, { timeZoneName: 'short' }) : 'Not recorded';
  const viewState = window.OpsGraphViewState;
  const terminal = status => ['completed', 'failed', 'blocked', 'interrupted', 'cancelled'].includes(status);
  const state = { authenticated: false, sources: [], skills: [], runs: [], run: null, policy: null, lastEvent: null, busy: false, modelTested: false, stream: null, streamToken: 0, lastEventId: 0, pending: null, activeDrawer: null, returnFocus: null, savedSkill: null, authEpoch: 0, sourceSetupToken: 0 };
  const key = () => { const value = sessionStorage.getItem('opsgraph.workspaceKey'); return validWorkspaceKey(value) ? value : ''; };
  // A launcher supplies only a short-lived single-use token, never the API key.
  const launchToken = new URLSearchParams(location.hash.slice(1)).get('connect');
  if (launchToken !== null) history.replaceState(null, '', location.pathname + location.search);

  function validWorkspaceKey(value) {
    return typeof value === 'string' && /^[\x20-\x7e]+$/.test(value) && Boolean(value.trim());
  }
  function runId(value) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(value)) {
      throw new Error('Invalid saved investigation identifier. Select an investigation from history.');
    }
    return value;
  }
  function apiPath(value) {
    if (typeof value !== 'string' || !value.startsWith('/api/') || /[\\\x00-\x20\x7f#]/.test(value)) {
      throw new Error('Invalid backend request path. Reload OpsGraph and retry.');
    }
    const url = new URL(value, location.origin);
    const segments = value.split('?')[0].split('/');
    if (url.origin !== location.origin || url.pathname !== value.split('?')[0] || segments.some(part => {
      const decoded = decodeURIComponent(part);
      return decoded === '.' || decoded === '..' || /[\/\\\x00-\x1f\x7f]/.test(decoded);
    })) throw new Error('Invalid backend request path. Reload OpsGraph and retry.');
    return url.href;
  }

  function notice(id, message = '') { const node = $(id); node.textContent = message; node.hidden = !message; }
  function preserveFocus(update) {
    const bookmark = viewState.focusBookmark(document);
    update();
    if (!state.activeDrawer && bookmark.node && !bookmark.node.isConnected) viewState.focusTarget(bookmark, document)?.focus({ preventScroll: true });
  }
  function guardAsyncFocus(control, fallback = null) {
    if (document.activeElement !== control) return () => {};
    let moved = false;
    const track = event => {
      if (event.type !== 'focusin' || (event.target !== control && event.target !== document.body)) moved = true;
    };
    const events = ['focusin', 'pointerdown', 'keydown'];
    events.forEach(type => document.addEventListener(type, track, true));
    return () => {
      events.forEach(type => document.removeEventListener(type, track, true));
      if (moved || document.activeElement !== document.body) return;
      const available = node => node?.isConnected && !node.disabled && node.getClientRects().length;
      const target = available(control) ? control : available(fallback) ? fallback : null;
      if (!target) return;
      if (target === fallback && !target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    };
  }
  function errorMessage(payload, fallback) {
    if (typeof payload.detail === 'string') return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map(item => `${(item.loc || []).filter(part => part !== 'body').join('.')}: ${item.msg}`).join('\n');
    return fallback;
  }
  async function api(path, options = {}) {
    const requestPath = apiPath(path);
    const requestKey = key();
    if (!requestKey) throw new Error('Connect your workspace using its key first.');
    let response;
    try {
      response = await fetch(requestPath, { ...options, redirect: 'error', headers: { Accept: 'application/json', 'X-OpsGraph-Key': requestKey, ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('Cannot reach OpsGraph. Check the backend and network, then retry or reopen the investigation. A lost connection does not tell us whether an operation was accepted.');
    }
    const payload = await response.json().catch(() => ({}));
    if (key() !== requestKey) throw new Error('Workspace connection changed; the previous response was discarded.');
    if (!response.ok) {
      const error = new Error(errorMessage(payload, `Request failed (${response.status}). Retry or check the backend connection.`));
      if (response.status === 401) error.message = 'Workspace key was rejected. Reconnect using the key configured on the backend.';
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  function showView(name, focus = true) {
    $$('[data-view-panel]').forEach(panel => { panel.hidden = panel.dataset.viewPanel !== name; panel.classList.toggle('active', !panel.hidden); });
    $$('.nav-item').forEach(button => { const active = button.dataset.view === name; button.classList.toggle('active', active); active ? button.setAttribute('aria-current', 'page') : button.removeAttribute('aria-current'); });
    if (focus) { const heading = $(`[data-view-panel="${name}"] h1`); heading?.setAttribute('tabindex', '-1'); heading?.focus({ preventScroll: true }); }
    if (state.authenticated && name === 'audit') loadAudit().catch(error => notice('#globalError', error.message));
    if (state.authenticated && name === 'policies') loadPolicy().catch(error => notice('#globalError', error.message));
    if (state.authenticated && name === 'investigations') loadHistory().catch(error => notice('#globalError', error.message));
  }
  function openDrawer(id, trigger) {
    if (state.activeDrawer) closeDrawer(false);
    state.activeDrawer = document.getElementById(id);
    state.returnFocus = trigger?.node ? trigger : { node: trigger || document.activeElement, key: (trigger || document.activeElement)?.dataset?.focusKey || null };
    state.activeDrawer.classList.add('open'); state.activeDrawer.setAttribute('aria-hidden', 'false');
    $('#drawerBackdrop').hidden = false;
    $$('.app-shell, .trust-bar, .skip-link').forEach(node => { node.inert = true; });
    $('.drawer-close', state.activeDrawer).focus();
  }
  function closeDrawer(restore = true) {
    if (!state.activeDrawer) return;
    state.activeDrawer.classList.remove('open'); state.activeDrawer.setAttribute('aria-hidden', 'true');
    $('#drawerBackdrop').hidden = true; $$('.app-shell, .trust-bar, .skip-link').forEach(node => { node.inert = false; });
    const target = viewState.focusTarget(state.returnFocus, document);
    state.activeDrawer = null; state.returnFocus = null;
    if (restore) { if (target?.isConnected) target.focus(); else $('#workspace').focus(); }
  }
  function download(filename, value) {
    const url = URL.createObjectURL(new Blob([json(value)], { type: 'application/json' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = filename;
    document.body.append(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
  }
  function stopStream() { state.streamToken++; state.stream?.abort(); state.stream = null; }
  function readiness() {
    const ready = state.sources.filter(source => source.kind === 'postgresql' && source.status === 'ready');
    $('#openCredential').textContent = state.authenticated ? 'Workspace connected' : 'Connect workspace';
    $('#workspaceReadiness').textContent = state.authenticated ? 'Authenticated to this backend.' : 'Use the key created by your OpsGraph operator.';
    $('#sourceReadiness').textContent = ready.length ? `${ready.length} inspected PostgreSQL source${ready.length === 1 ? '' : 's'}.` : 'Configure and inspect an approved read-only source.';
    $('#modelReadiness').textContent = state.modelTested ? 'Actual model connection test passed in this tab.' : 'A real model connection test has not passed in this tab.';
    $('#saveSkill').disabled = !state.authenticated;
    const active = state.run && !terminal(state.run.status);
    $('#submitRun').disabled = state.busy || !state.authenticated || !ready.length || !state.modelTested || Boolean(active);
    $('#investigationSource').disabled = Boolean(state.run) || state.busy;
    $('#investigationSkill').disabled = Boolean(state.run) || state.busy;
    $('#investigationQuestion').disabled = state.busy || Boolean(active);
    $('#submitRun').textContent = state.busy ? 'Submitting…' : state.run ? 'Ask follow-up' : 'Start investigation';
    $('#composerTitle').textContent = state.run ? 'Ask a follow-up' : 'Start an investigation';
    $('#composerContext').textContent = active ? 'This investigation is still active. You can cancel it or wait for a terminal state.' : state.run ? 'Creates a linked run with this source and playbook. Previous evidence remains unchanged; new evidence has its own capture time.' : !state.authenticated ? 'Connect your workspace, inspect a source, and test your model before asking.' : !ready.length ? 'Configure a source in Sources. Model availability does not block source inspection.' : !state.modelTested ? 'Test the actual model connection in Settings before your first question.' : 'Choose an inspected source and a bounded operational question, including a time range where relevant.';
    if (state.run?.error?.code === 'clarification_required') {
      $('#composerTitle').textContent = 'Clarify your question';
      $('#composerContext').textContent = 'Answer the clarification above with the needed definitions, join keys or time rules. This creates a linked attempt; nothing is treated as completed evidence from the blocked attempt.';
    }
    $('#testProvider').disabled = !state.authenticated;
    renderComposerScope();
  }
  function scopeMarkup(fields) {
    return `<dl class="scope-summary-list">${Object.entries(fields).map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`;
  }
  function renderComposerScope() {
    const source = state.sources.find(item => item.id === $('#investigationSource').value);
    const skill = state.skills.find(item => item.id === ($('#investigationSkill').value || 'generic-readonly'));
    const preview = viewState.previewScope(source, skill, state.policy);
    if (!source) { $('#composerScope').textContent = 'Select a source to review its configured scope.'; return; }
    $('#composerScope').innerHTML = scopeMarkup({
      'Source': `${source.name} · ${source.id}`,
      'Source-approved tables': (source.allowed_tables || []).join(', ') || 'None — execution is blocked',
      'Source inspection': `${source.status} · ${source.inspected_at ? stamp(source.inspected_at) : 'Time unavailable'}`,
      'Selected playbook table scope': preview ? preview.tables.join(', ') || 'None — execution is blocked' : 'Not available until playbook and policy are loaded',
      'Query bounds preview': preview?.max_rows != null && preview?.timeout_ms != null ? `${preview.max_rows} rows per query · ${preview.timeout_ms} ms per query` : 'Unavailable',
    }) + `<p class="helper">Configuration preview; the backend revalidates permissions and records effective bounds when execution starts. Column access follows database grants or restricted views. No business meaning or time filter is implied by selecting a table.</p>${preview?.blocked ? `<p class="notice error">${esc(preview.blocked)}</p>` : ''}`;
  }
  async function loadBootstrap() {
    try {
      const response = await fetch('/api/bootstrap', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error('Backend configuration could not be read. Check that OpsGraph is running.');
      const bootstrap = await response.json(); const trust = bootstrap.trust || {};
      $('#runtimeDetails').textContent = json(bootstrap);
      $('#trustAccess').textContent = trust.access ? `${trust.access} · ${trust.deployment || 'backend'}` : 'Access unverified';
      $('#trustEgress').textContent = trust.egress === false ? 'External egress off' : trust.egress === true ? 'External egress enabled' : 'Egress unverified';
      $('#trustModel').textContent = trust.model ? `${trust.model} configured · untested` : 'Model unconfigured';
    } catch (error) { notice('#globalError', error.message); $('#runtimeDetails').textContent = error.message; }
  }
  async function loadProvider() {
    const current = await api('/api/providers/current'); const health = current.health || {};
    $('#providerConfig').innerHTML = `<div><dt>Configured provider</dt><dd>${esc(health.provider || 'Not reported')}</dd></div><div><dt>Configured model</dt><dd>${esc(health.model || 'Not reported')}</dd></div><div><dt>Configuration status</dt><dd>${esc(health.status || 'Unknown')}: ${esc(health.detail || 'No detail reported')}</dd></div><div><dt>External inference</dt><dd>${current.capabilities?.external_egress === true ? 'Configured provider uses external egress' : current.capabilities?.external_egress === false ? 'Provider reports no external egress' : 'Not reported'}</dd></div>`;
    return current;
  }
  async function testProvider() {
    const restoreFocus = guardAsyncFocus($('#testProvider'), $('#providerTestStatus'));
    notice('#providerError'); $('#testProvider').disabled = true; $('#providerTestStatus').textContent = 'Running an actual bounded model request…';
    state.modelTested = false; readiness(); $('#testProvider').disabled = true;
    try {
      const result = await api('/api/providers/current/test', { method: 'POST' });
      state.modelTested = result.ok === true;
      if (!state.modelTested) throw new Error(result.health?.detail || 'Model test failed. Check the model runtime, configured model and network address.');
      $('#providerTestStatus').textContent = `Real connection test passed: ${result.provider || result.health?.provider || 'configured provider'} / ${result.model || result.health?.model || 'configured model'} · ${stamp(result.checked_at)}.`;
      $('#trustModel').textContent = 'Actual model test passed';
      await loadProvider();
    } catch (error) {
      notice('#providerError', error.message); $('#providerTestStatus').textContent = 'Model unavailable. Source setup and saved investigations remain available.'; $('#trustModel').textContent = 'Model test failed';
    } finally { readiness(); restoreFocus(); }
  }
  async function loadSources() {
    state.sources = (await api('/api/sources')).filter(source => source.kind === 'postgresql');
    const selected = state.run?.source_id || $('#investigationSource').value;
    const ready = state.sources.filter(source => source.status === 'ready');
    $('#investigationSource').innerHTML = '<option value="">Select an inspected source</option>' + ready.map(source => `<option value="${esc(source.id)}">${esc(source.name)}${ready.some(other => other.id !== source.id && other.name === source.name) ? ` · ${esc(source.id)}` : ''}</option>`).join('');
    if (state.run && !ready.some(source => source.id === selected)) $('#investigationSource').insertAdjacentHTML('beforeend', `<option value="${esc(selected)}">${esc(selected)} · unavailable</option>`);
    if (selected) $('#investigationSource').value = selected;
    else if (ready.length === 1) $('#investigationSource').value = ready[0].id;
    $('#sourceCatalog').innerHTML = state.sources.map(source => `<article class="source-card"><p class="eyebrow">POSTGRESQL · ${esc(source.status)}</p><h2>${esc(source.name)}</h2><p>${esc(source.id)}</p><p>${esc((source.allowed_tables || []).join(', ') || 'No explicit table scope')}</p><p>${source.allow_external_egress ? 'External inference permitted by source' : 'Local inference only'}</p><button class="secondary" data-edit-source="${esc(source.id)}">Configure / inspect</button></article>`).join('') || '<p class="helper">No PostgreSQL sources configured. Add a source to begin.</p>';
    readiness();
  }
  async function loadSkills() {
    state.skills = await api('/api/skills');
    const selected = state.run?.skill_id || $('#investigationSkill').value;
    $('#investigationSkill').innerHTML = '<option value="">General read-only</option>' + state.skills.filter(skill => skill.id !== 'generic-readonly').map(skill => `<option value="${esc(skill.id)}">${esc(skill.name)} · ${esc(skill.version)}</option>`).join('');
    if (selected) $('#investigationSkill').value = selected === 'generic-readonly' ? '' : selected;
    $('#skillCatalog').innerHTML = state.skills.map(skill => `<article><p class="eyebrow">${esc(skill.id)} · ${esc(skill.version)}</p><h2>${esc(skill.name)}</h2><p>${esc(skill.purpose)}</p><p>Required evidence: ${esc((skill.required_evidence || []).join(', ') || 'No specialist evidence types')}</p><p>Tools: ${esc((skill.tools || []).map(binding => binding.tool).join(', '))}</p><button class="secondary" data-view="sources">Configure source mappings</button></article>`).join('') || '<p>No published playbooks available.</p>';
    renderComposerScope();
  }
  async function loadHistory() { state.runs = await api('/api/runs'); renderHistory(); }
  function renderHistory() {
    const query = $('#caseSearch').value.trim().toLowerCase();
    const runs = state.runs.filter(run => `${run.question} ${run.source_id}`.toLowerCase().includes(query));
    $('#historyMessage').textContent = !state.authenticated ? 'Connect workspace to load saved investigations.' : !state.runs.length ? 'No investigations yet.' : !runs.length ? 'No matching investigations.' : `${state.runs.length} saved investigation${state.runs.length === 1 ? '' : 's'}.`;
    preserveFocus(() => { $('#caseList').innerHTML = runs.map(run => `<button class="case-card${state.run?.id === run.id ? ' active' : ''}" data-run-id="${esc(run.id)}" data-focus-key="history:${esc(run.id)}"><span class="case-state">${esc(run.status)}</span><b>${esc(run.question)}</b><p>${esc(run.source_id)}</p><time datetime="${esc(run.created_at)}">${esc(stamp(run.created_at))}</time></button>`).join(''); });
  }
  async function loadPolicy() { state.policy = await api('/api/policies/current'); $('#policyDetails').textContent = json(state.policy); renderComposerScope(); }
  async function loadAudit() { $('#auditDetails').textContent = json(await api('/api/audit')); }
  async function loadWorkspace() {
    state.authenticated = true; notice('#globalError');
    const results = await Promise.allSettled([loadSources(), loadSkills(), loadProvider(), loadHistory(), loadPolicy()]);
    const rejected = results.filter(item => item.status === 'rejected');
    if (rejected.some(item => item.reason.status === 401)) { state.authenticated = false; throw rejected.find(item => item.reason.status === 401).reason; }
    if (rejected.length) notice('#globalError', rejected.map(item => item.reason.message).join('\n'));
    readiness();
  }
  async function connectWorkspace(event) {
    const restoreFocus = guardAsyncFocus($('#saveCredential'), $('#workspace'));
    event.preventDefault(); const epoch = state.authEpoch; notice('#credentialError'); const entered = $('#workspaceKey').value.trim();
    $('#saveCredential').disabled = true;
    try {
      if (!validWorkspaceKey(entered)) throw new Error('Workspace key must be a nonempty printable ASCII value.');
      const response = await fetch('/api/sources', { headers: { 'X-OpsGraph-Key': entered, Accept: 'application/json' } });
      if (!response.ok) throw new Error(response.status === 401 ? 'Workspace key rejected. Use the key configured on this backend.' : `Could not connect (${response.status}). Check the backend and try again.`);
      if (epoch !== state.authEpoch) return;
      sessionStorage.setItem('opsgraph.workspaceKey', entered); $('#workspaceKey').value = ''; await loadWorkspace(); closeDrawer();
      const saved = sessionStorage.getItem('opsgraph.selectedRun');
      if (saved) await openRun(saved);
    } catch (error) { notice('#credentialError', error.message); }
    finally { $('#saveCredential').disabled = false; readiness(); restoreFocus(); }
  }
  function clearWorkspace() {
    state.authEpoch++; stopStream(); sessionStorage.removeItem('opsgraph.workspaceKey'); sessionStorage.removeItem('opsgraph.selectedRun');
    state.authenticated = false; state.modelTested = false; state.sources = []; state.skills = []; state.runs = []; state.run = null; state.policy = null; state.lastEvent = null; state.sourceSetupToken++; state.pending = null; state.savedSkill = null; $('#skillForm').reset(); $('#skillStatus').textContent = ''; $('#publishSkill').disabled = true;
    $('#workspaceKey').value = ''; $('#caseList').replaceChildren(); $('#findingGrid').replaceChildren(); $('#evidenceDetail').replaceChildren(); $('#activityLog').replaceChildren(); $('#runWorkspace').hidden = true; $('#onboarding').hidden = false;
    $('#investigationSource').innerHTML = '<option value="">Connect a source first</option>'; $('#investigationSkill').innerHTML = '<option value="">General read-only</option>';
    $('#investigationQuestion').value = ''; $('#sourceCatalog').textContent = 'Connect workspace to load sources.'; $('#skillCatalog').textContent = 'Connect workspace to load playbooks.';
    $('#policyDetails').textContent = 'Connect workspace to load policy.'; $('#auditDetails').textContent = 'Connect workspace to load audit records.'; $('#providerConfig').textContent = 'Connect workspace to read configuration.';
    $('#trustModel').textContent = 'Model unchecked';
    ['#previousTurn', '#caseTitle', '#runIdentity', '#runRelation', '#runSource', '#runCreated', '#runSkill', '#runState', '#conclusionTitle', '#limitations', '#evidenceLedger', '#streamState', '#runScope', '#runScopeSummary', '#currentOperation', '#captureStatus', '#inspectedTables'].forEach(id => $(id).replaceChildren());
    $('#providerTestStatus').textContent = 'No connection test performed in this tab.'; $('#sourceSetup').hidden = true; $('#sourceForm').reset();
    ['#globalError', '#composerError', '#credentialError', '#sourceError', '#providerError', '#skillError'].forEach(id => notice(id));
    renderHistory(); readiness(); closeDrawer();
  }
  function sourceSetup(source = null) {
    const token = ++state.sourceSetupToken;
    showView('sources'); $('#sourceSetup').hidden = false; $('#sourceForm').reset(); $('#inspectedTables').hidden = true; $('#inspectedTables').replaceChildren(); notice('#sourceError'); $('#sourceStatus').textContent = '';
    $('#sourceId').readOnly = Boolean(source);
    if (!source) {
      $('#sourceId').value = `source-${Array.from(crypto.getRandomValues(new Uint8Array(4)), byte => byte.toString(16).padStart(2, '0')).join('')}`;
      $('#sourceName').value = 'PostgreSQL read-only';
      $('#sourceSecretRef').value = 'OPSGRAPH_SOURCE_DSN';
      $('#sourceSchemas').value = (state.policy?.obligations?.allowed_schemas || ['public']).join(', ');
    }
    if (source) {
      $('#sourceId').value = source.id; $('#sourceName').value = source.name; $('#sourceSecretRef').value = source.secret_ref || '';
      $('#sourceSchemas').value = (source.allowed_schemas || []).join(', '); $('#sourceTables').value = (source.allowed_tables || []).join(', ');
      $('#sourceEvidenceBindings').value = (source.evidence_bindings || []).map(binding => `${binding.evidence_type}=${binding.source_tables.join(',')}`).join('\n');
      $('#sourceExternalEgress').checked = source.allow_external_egress === true;
      $('#sourceStatus').textContent = 'Loading saved inspection; no database query is being run.';
      api(`/api/sources/${encodeURIComponent(source.id)}/schema`).then(snapshot => {
        if (token !== state.sourceSetupToken) return;
        renderInspection(snapshot);
        $('#sourceStatus').textContent = 'Saved inspection loaded. Save and inspect again to refresh database metadata.';
      }).catch(error => {
        if (token !== state.sourceSetupToken) return;
        $('#sourceStatus').textContent = 'Saved inspection unavailable. Save and inspect this source to check current access.';
        if (error.status !== 404) notice('#sourceError', error.message);
      });
    }
    $(source ? '#sourceName' : '#sourceTables').focus();
  }
  function renderInspection(snapshot) {
    const allowed = new Set($('#sourceTables').value.split(',').map(item => item.trim()).filter(Boolean));
    const tables = (snapshot.tables || []).filter(table => allowed.has(`${table.schema_name}.${table.table_name}`));
    const warnings = snapshot.type_warnings || [];
    const limits = snapshot.metadata_limits || ['Relationships and join cardinality are not discovered.', 'Business meanings, units, status definitions and business time semantics are not configured.'];
    $('#inspectedTables').innerHTML = `<h3>Inspected physical schema</h3>${scopeMarkup({ 'Inspection status': snapshot.status || 'Unavailable', 'Last inspected': snapshot.inspected_at ? stamp(snapshot.inspected_at) : 'Unavailable for this saved inspection', 'Schema fingerprint': snapshot.fingerprint || 'Unavailable' })}<p class="helper">Saved metadata from an actual inspection, not a continuous schema monitor. Uncheck a table to narrow scope, then save and inspect again. Newly discovered tables are never added automatically.</p>${snapshot.status === 'stale' ? '<p class="notice error">The database schema changed after inspection. Review these saved columns, then save and inspect again before investigating.</p>' : ''}<p class="helper">Every column visible to the database role in an allowed table can be queried. Restrict columns with database grants or an approved read-only view; these checkboxes restrict tables only.</p>${tables.map(table => {
      const name = `${table.schema_name}.${table.table_name}`;
      return `<section class="schema-table"><label class="checkbox-label"><input type="checkbox" data-inspected-table="${esc(name)}" checked><span>${esc(name)}</span></label><details open><summary>Physical columns (${(table.columns || []).length})</summary><div class="evidence-table-wrap" tabindex="0" role="region" aria-label="${esc(name)} columns, horizontally scrollable"><table class="evidence-table"><thead><tr><th scope="col">Column</th><th scope="col">PostgreSQL type</th><th scope="col">Nullable</th></tr></thead><tbody>${(table.columns || []).map(column => `<tr><td>${esc(column.name)}</td><td>${esc(column.data_type)}</td><td>${column.nullable === true ? 'Yes' : column.nullable === false ? 'No' : 'Unavailable'}</td></tr>`).join('')}</tbody></table></div></details></section>`;
    }).join('') || '<p>No permitted tables were retained in this inspection.</p>'}${warnings.length ? `<div class="notice error"><b>Unsupported or limited result types</b><ul>${warnings.map(warning => `<li>${esc(warning.table)}.${esc(warning.column)} (${esc(warning.data_type)}): ${esc(warning.message)}</li>`).join('')}</ul></div>` : ''}<p class="helper">Physical names and data types do not establish business meaning. Include necessary definitions, units, status meanings, join keys and timezone/time-range rules in your question; ask for clarification when unsure.</p><ul class="helper">${limits.map(limit => `<li>${esc(limit)}</li>`).join('')}</ul>`;
    $('#inspectedTables').hidden = false;
  }
  async function saveSource(event) {
    const restoreFocus = guardAsyncFocus($('#saveSource'), $('#sourceStatus'));
    const token = ++state.sourceSetupToken;
    event.preventDefault(); notice('#sourceError'); $('#saveSource').disabled = true; $('#sourceStatus').textContent = 'Saving source configuration…';
    const parts = value => value.split(',').map(item => item.trim()).filter(Boolean);
    const id = $('#sourceId').value.trim();
    try {
      const bindings = $('#sourceEvidenceBindings').value.split(/\n|;/).map(line => line.trim()).filter(Boolean).map(line => {
        const [type, tables] = line.split('=', 2);
        if (!type?.trim() || !tables?.trim()) throw new Error('Use evidence_type=public.table for each playbook mapping.');
        return { evidence_type: type.trim(), source_tables: parts(tables) };
      });
      await api('/api/sources', { method: 'POST', body: JSON.stringify({ id, name: $('#sourceName').value.trim(), secret_ref: $('#sourceSecretRef').value.trim(), allowed_schemas: parts($('#sourceSchemas').value), allowed_tables: parts($('#sourceTables').value), evidence_bindings: bindings, allow_external_egress: $('#sourceExternalEgress').checked }) });
      if (token !== state.sourceSetupToken) return;
      $('#sourceStatus').textContent = 'Configuration saved. Checking actual database access and inspecting permitted schema…';
      const snapshot = await api(`/api/sources/${encodeURIComponent(id)}/inspect`, { method: 'POST' });
      if (token !== state.sourceSetupToken) return;
      renderInspection(snapshot);
      $('#sourceStatus').textContent = `Inspection succeeded: ${(snapshot.tables || []).length} permitted table${(snapshot.tables || []).length === 1 ? '' : 's'}. Model readiness is checked separately.`;
      await loadSources();
    } catch (error) { if (token !== state.sourceSetupToken) return; $('#sourceStatus').textContent = 'Source is not ready for a new investigation.'; notice('#sourceError', `${error.message}\nCheck the backend environment variable, read-only grants, and explicit table/mapping scope. Save and inspect again after correcting configuration.`); await loadSources().catch(() => {}); }
    finally { $('#saveSource').disabled = false; restoreFocus(); }
  }

  function renderRun(run) {
    const bookmark = viewState.focusBookmark(document);
    const selectedId = runId(run.id);
    state.run = run; sessionStorage.setItem('opsgraph.selectedRun', selectedId);
    $('#onboarding').hidden = true; $('#runWorkspace').hidden = false;
    $('#runIdentity').textContent = run.legacy_provenance ? `${run.id} · imported legacy investigation; some provenance was not retained` : run.id; $('#caseTitle').textContent = run.question; $('#runSource').textContent = run.source_id;
    $('#runCreated').textContent = stamp(run.created_at); $('#runSkill').textContent = run.skill_id || 'Selection pending'; $('#runState').textContent = run.status;
    const configuration = run.configuration;
    const recordedTables = configuration?.limits?.allowed_tables || [];
    $('#runScopeSummary').textContent = configuration ? `Recorded scope · ${recordedTables.slice(0, 2).join(', ') || 'tables unavailable'}${recordedTables.length > 2 ? ` + ${recordedTables.length - 2} more` : ''}${configuration.limits?.max_rows != null ? ` · ${configuration.limits.max_rows} rows/query` : ''}${configuration.limits?.timeout_ms != null ? ` · ${configuration.limits.timeout_ms} ms/query` : ''}` : 'Recorded investigation scope · unavailable';
    $('#runScope').innerHTML = configuration ? scopeMarkup({
      'Recorded source': [configuration.source_name, configuration.source_id].filter(Boolean).join(' · ') || 'Unavailable',
      'Permitted tables at execution': (configuration.limits?.allowed_tables || []).join(', ') || 'Unavailable in this record',
      'Recorded row bound': configuration.limits?.max_rows != null ? `${configuration.limits.max_rows} rows per query` : 'Unavailable',
      'Recorded query timeout': configuration.limits?.timeout_ms != null ? `${configuration.limits.timeout_ms} ms per query` : 'Unavailable',
      'Maximum queries': configuration.max_queries ?? 'Unavailable in this record',
      'Source configuration revision': configuration.source_revision || 'Unavailable',
      'Schema fingerprint': configuration.schema_fingerprint || 'Unavailable',
      'Schema inspected': configuration.schema_inspected_at ? stamp(configuration.schema_inspected_at) : 'Time unavailable in this record',
      'Playbook version': configuration.skill_version || 'Unavailable',
    }) + '<p class="helper">These are this attempt’s recorded execution bounds. Current source settings may differ. Column permissions are enforced by PostgreSQL; schema names do not establish business meaning.</p>' : '<p class="helper">Execution scope has not been recorded for this attempt. Historical records may lack these details; current settings do not establish historical scope.</p>';
    $('#currentOperation').textContent = viewState.currentOperation(run, state.lastEvent);
    const previousId = run.retry_of || run.parent_run_id;
    const previous = state.runs.find(item => item.id === previousId);
    const previousExpanded = $('#previousTurn').dataset.runId === run.id && $('#previousTurn details')?.open;
    $('#previousTurn').dataset.runId = run.id;
    $('#previousTurn').hidden = !previousId;
    $('#previousTurn').innerHTML = previousId ? `<details${previousExpanded ? ' open' : ''}><summary data-focus-key="previous-summary:${esc(run.id)}">${run.retry_of ? 'Previous attempt' : 'Previous conversation turn'} · historical context</summary>${previous ? `<p><strong>${esc(previous.question)}</strong></p><p>${esc(previous.answer?.summary || `Saved state: ${previous.status}. No completed conclusion recorded.`)}</p><p class="helper">Source ${esc(previous.source_id)} · ${esc(stamp(previous.created_at))}. Previous claims remain assessments; this run collects its own evidence.</p>` : '<p class="helper">Open the saved previous turn to inspect its question, state and evidence.</p>'}<button class="secondary" data-run-id="${esc(previousId)}" data-focus-key="previous:${esc(run.id)}">Open previous ${run.retry_of ? 'attempt' : 'turn'}</button></details>` : '';
    $('#runRelation').textContent = run.retry_of ? `New attempt of ${run.retry_of}. Previous attempt retained.` : run.parent_run_id ? `Follow-up to ${run.parent_run_id}. Previous evidence retained.` : '';
    $('#cancelRun').hidden = terminal(run.status); $('#cancelRun').disabled = run.status === 'cancelling'; $('#cancelRun').textContent = run.status === 'cancelling' ? 'Cancellation requested' : 'Cancel run';
    $('#retryRun').hidden = !['failed', 'blocked', 'interrupted', 'cancelled'].includes(run.status);
    notice('#runError', run.error ? `${run.error.message || run.error.code}${run.status === 'interrupted' ? '\nBackend process stopped. Retry explicitly to create a separate attempt; this run will not resume automatically.' : ''}` : run.status === 'cancelling' ? 'Cancellation requested. The backend must finish or interrupt its current bounded operation before cancellation is confirmed.' : '');
    const answer = run.answer; $('#conclusionCard').hidden = !answer;
    $('#conclusionTitle').textContent = answer?.summary || '';
    $('#limitations').innerHTML = (answer?.limitations || []).map(limit => `<li>${esc(limit)}</li>`).join('') || (answer ? '<li>No additional limitation recorded by the model. This does not establish completeness.</li>' : '');
    const classes = new Set(['supported', 'possible', 'unknown', 'contradictory']);
    $('#findingGrid').innerHTML = (answer?.findings || []).map((finding, index) => {
      const classification = classes.has(finding.classification) ? finding.classification : 'unknown';
      return `<article class="finding"><div class="finding-head"><span class="classification ${classification}">${esc(classification.toUpperCase())} · MODEL ASSESSMENT</span><small>${(finding.evidence_ids || []).length} references</small></div><h3>${esc(finding.claim)}</h3><p class="helper">${esc(viewState.classificationExplanation(classification))} Review the captured rows; classification is not independently verified.</p>${(finding.evidence_ids || []).length ? `<button class="citation-button" data-finding="${index}" data-focus-key="finding:${esc(run.id)}:${index}">Inspect referenced evidence →</button>` : '<p>No referenced evidence. Treat this claim as unsupported.</p>'}</article>`;
    }).join('');
    $('#evidenceSection').hidden = !(run.evidence || []).length;
    $('#toggleEvidence').textContent = `Inspect evidence (${(run.evidence || []).length} captures)`;
    $('#captureStatus').textContent = viewState.captureStatus(run);
    $('#evidenceLedger').innerHTML = (run.evidence || []).map((item, index) => `<button class="evidence-reference" data-evidence="${index}" data-focus-key="capture:${esc(run.id)}:${esc(item.provenance?.capture_id || item.evidence_hash || index)}"><b>${esc(item.purpose || `Capture ${index + 1}`)}</b><span>${run.status === 'completed' ? 'Recorded capture' : 'Partial evidence'} · ${esc(item.provenance?.source_id || run.source_id)} · collected ${esc(stamp(item.provenance?.finished_at || item.created_at))} · ${(item.rows || []).length} rows${item.truncated ? ' · truncated' : ''}</span></button>`).join('');
    if (terminal(run.status)) $('#streamState').textContent = `Saved ${run.status} state · updated ${stamp(run.updated_at)}.`;
    const option = [...$('#investigationSource').options].find(item => item.value === run.source_id);
    if (!option) $('#investigationSource').insertAdjacentHTML('beforeend', `<option value="${esc(run.source_id)}">${esc(run.source_id)}</option>`);
    $('#investigationSource').value = run.source_id; $('#investigationSkill').value = run.skill_id === 'generic-readonly' ? '' : run.skill_id || '';
    const existing = state.runs.findIndex(item => item.id === run.id);
    if (existing < 0) state.runs.unshift(run); else state.runs[existing] = run;
    renderHistory(); readiness();
    if (!state.activeDrawer && bookmark.node && !bookmark.node.isConnected) viewState.focusTarget(bookmark, document)?.focus({ preventScroll: true });
  }
  function evidenceMarkup(item, run) {
    const provenance = item.provenance || {};
    const fields = { 'Source identity': provenance.source_id || run.source_id || 'Not recorded', 'Capture ID': provenance.capture_id || 'Not recorded', 'Collection started': stamp(provenance.started_at), 'Collection finished': stamp(provenance.finished_at || item.created_at), 'Requested time range': 'As stated in the question and enforced by the recorded SQL; no additional time filter is implied.', 'Evidence hash': item.evidence_hash || 'Not recorded', 'Query fingerprint': item.query_fingerprint || 'Not recorded', 'Source tables': (item.referenced_tables || []).join(', ') || 'Not recorded', 'Rows retained': (item.rows || []).length, 'Effective query row limit': provenance.limits?.max_rows ?? 'Not recorded', 'Effective query timeout': provenance.limits?.timeout_ms != null ? `${provenance.limits.timeout_ms} ms` : 'Not recorded', 'Permitted tables for this capture': (provenance.limits?.allowed_tables || []).join(', ') || 'Not recorded', 'Playbook version': provenance.skill_version || 'Not recorded', 'Truncated': item.truncated === true ? 'Yes — additional rows may exist' : item.truncated === false ? 'No truncation reported; this does not prove complete source coverage' : 'Not recorded', 'Configured model': [provenance.provider, provenance.model].filter(Boolean).join(' / ') || 'Not recorded', 'Planner model (reported)': provenance.reported_model || 'Unavailable', 'Schema fingerprint': provenance.schema_fingerprint || 'Not recorded' };
    const integrity = item.integrity;
    const integrityMarkup = integrity?.format === 'opsgraph-canonical-json-v1' && typeof integrity.canonical_json === 'string'
      ? `<details><summary>Inspect exact content-hash input</summary><p class="helper">SHA-256 of the UTF-8 text below should equal the evidence hash. This covers query-result fields, including typed values, not capture provenance or the model's interpretation.</p><pre><code>${esc(integrity.canonical_json)}</code></pre></details>`
      : '<p class="helper">Exact canonical hash input was not retained for this historical capture. Typed values may prevent independently reconstructing its hash from displayed rows.</p>';
    const columns = item.columns || [];
    const rows = item.rows || [];
    return `<p class="helper">${esc(viewState.captureStatus(run))}</p><p class="helper">Captured database evidence. Hashes identify recorded bytes; they do not verify the model's interpretation.</p><dl class="detail-list">${Object.entries(fields).map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>${integrityMarkup}<h3>Executed query</h3><pre><code>${esc(provenance.sql || 'Exact SQL was not retained for this capture. Do not infer it from a different query.')}</code></pre><h3>Captured rows</h3>${columns.length ? `<div class="evidence-table-wrap" tabindex="0" role="region" aria-label="Captured rows, horizontally scrollable"><table class="evidence-table"><thead><tr>${columns.map(column => `<th scope="col">${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map((column, index) => { const value = Array.isArray(row) ? row[index] : row[column]; return `<td>${esc(typeof value === 'object' && value !== null ? JSON.stringify(value) : value ?? 'null')}</td>`; }).join('')}</tr>`).join('')}</tbody></table></div>` : `<pre>${esc(json(rows))}</pre>`}`;
  }
  function showEvidence(index, trigger) {
    const item = state.run?.evidence?.[index]; if (!item) return;
    $('#evidenceDrawerTitle').textContent = item.purpose || 'Evidence capture'; $('#evidenceDetail').innerHTML = evidenceMarkup(item, state.run); openDrawer('evidenceDrawer', trigger);
  }
  function showFinding(index, trigger) {
    const finding = state.run?.answer?.findings?.[index]; if (!finding) return;
    const references = finding.evidence_ids || [];
    $('#evidenceDrawerTitle').textContent = 'Claim and referenced evidence';
    $('#evidenceDetail').innerHTML = `<h3>${esc(finding.claim)}</h3><p class="helper">${esc(viewState.classificationExplanation(finding.classification))} References below do not independently establish support, contradiction or causality.</p><p class="helper">A separate explanation linking this classification to particular rows was not recorded. Inspect the query, values and limitations; the application does not infer that relationship from citation membership.</p>` + references.map(hash => {
      const matches = (state.run.evidence || []).map((item, itemIndex) => ({ item, itemIndex })).filter(({ item }) => item.evidence_hash === hash);
      return matches.length ? matches.map(({ item, itemIndex }) => `<button class="evidence-reference" data-evidence="${itemIndex}"><b>${esc(item.purpose || 'Recorded capture')}</b><span>${esc(hash)} · ${esc(stamp(item.provenance?.finished_at || item.created_at))}</span></button>`).join('') : `<p class="notice error">Referenced evidence unavailable: ${esc(hash)}. Claim cannot be verified from this result.</p>`;
    }).join('');
    openDrawer('evidenceDrawer', trigger);
  }
  async function openRun(id) {
    stopStream(); const token = state.streamToken; notice('#composerError'); notice('#globalError');
    try {
      id = runId(id);
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (token !== state.streamToken) return;
      $('#activityLog').replaceChildren(); state.lastEventId = 0; state.lastEvent = null; $('#investigationQuestion').value = ''; renderRun(run); showView('investigations', false); streamRun(id, token);
    } catch (error) { notice('#globalError', error.message); }
  }
  function addEvent(event) {
    const item = document.createElement('li'); const time = document.createElement('time');
    time.dateTime = event.created_at || ''; time.textContent = stamp(event.created_at);
    const label = document.createElement('span');
    label.textContent = viewState.eventLabel(event); state.lastEvent = viewState.operationEvent(state.lastEvent, event);
    if (state.run?.id === event.run_id) $('#currentOperation').textContent = viewState.currentOperation(state.run, state.lastEvent);
    item.append(time, label); $('#activityLog').append(item);
  }
  async function streamRun(id, token) {
    let delay = 1000;
    while (token === state.streamToken && state.authenticated) {
      const controller = new AbortController(); state.stream = controller;
      try {
        const response = await fetch(apiPath(`/api/runs/${encodeURIComponent(runId(id))}/events?after=${state.lastEventId}`), { redirect: 'error', headers: { Accept: 'text/event-stream', 'X-OpsGraph-Key': key() }, signal: controller.signal });
        if (!response.ok || !response.body) { const error = new Error(`Event connection failed (${response.status}).`); error.status = response.status; throw error; }
        $('#streamState').classList.remove('disconnected'); $('#streamState').textContent = 'Connected to recorded backend activity.'; delay = 1000;
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = Promise.resolve(); let refreshError = null;
        const parser = window.OpsGraphEvents.createSSEParser(frame => {
          const event = JSON.parse(frame.data); const sequence = Number(frame.id || event.id);
          if (!Number.isSafeInteger(sequence) || sequence <= state.lastEventId || event.run_id !== id) return;
          state.lastEventId = sequence; addEvent(event);
          pending = pending.then(async () => { if (token !== state.streamToken) return; const run = await api(`/api/runs/${encodeURIComponent(id)}`); if (token === state.streamToken) renderRun(run); }).catch(error => { refreshError = error; });
        });
        while (true) { const { value, done } = await reader.read(); if (done) break; if (token !== state.streamToken) { await reader.cancel(); return; } parser.push(decoder.decode(value, { stream: true })); }
        parser.push(decoder.decode()); parser.finish(); await pending; if (refreshError) throw refreshError;
        if (token !== state.streamToken) return;
        const run = await api(`/api/runs/${encodeURIComponent(id)}`); if (token !== state.streamToken) return; renderRun(run);
        if (terminal(run.status)) return;
        throw new Error('Event connection ended while this run remains active.');
      } catch (error) {
        if (token !== state.streamToken || error.name === 'AbortError') return;
        $('#streamState').classList.add('disconnected');
        $('#streamState').textContent = error.status === 401 ? 'Event access rejected. Reconnect your workspace key; this does not cancel execution.' : 'Connection lost. Reconnecting to this run; no new investigation is being submitted. Last recorded state remains visible.';
        if (error.status === 401 || error.status === 404) { notice('#globalError', error.message); return; }
        await new Promise(resolve => setTimeout(resolve, delay)); delay = Math.min(delay * 2, 10000);
      }
    }
  }
  async function submitRun(event) {
    event.preventDefault(); if (state.busy || $('#submitRun').disabled) return;
    const restoreFocus = guardAsyncFocus($('#submitRun'), $('#currentOperation'));
    notice('#composerError'); state.busy = true; readiness();
    const body = { question: $('#investigationQuestion').value.trim(), source_id: $('#investigationSource').value, skill_id: $('#investigationSkill').value || null, parent_run_id: state.run?.id || null };
    const signature = JSON.stringify(body);
    if (!state.pending || state.pending.signature !== signature) state.pending = { signature, request_id: typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('') };
    body.request_id = state.pending.request_id;
    try { const run = await api('/api/runs', { method: 'POST', body: JSON.stringify(body) }); state.pending = null; await openRun(run.id); }
    catch (error) { notice('#composerError', `${error.message}\nCorrect the source/model configuration or retry this same question. A retry of this submission uses the same request ID.`); }
    finally { state.busy = false; readiness(); restoreFocus(); }
  }
  async function cancelRun() {
    if (!state.run || terminal(state.run.status)) return;
    const restoreFocus = guardAsyncFocus($('#cancelRun'), $('#currentOperation'));
    $('#cancelRun').disabled = true; notice('#runError');
    try { renderRun(await api(`/api/runs/${encodeURIComponent(state.run.id)}/cancel`, { method: 'POST' })); }
    catch (error) { notice('#runError', error.message); $('#cancelRun').disabled = false; }
    finally { restoreFocus(); }
  }
  async function retryRun() {
    if (!state.run || state.busy) return;
    const restoreFocus = guardAsyncFocus($('#retryRun'), $('#currentOperation'));
    state.busy = true; $('#retryRun').disabled = true;
    try { const run = await api(`/api/runs/${encodeURIComponent(state.run.id)}/retry`, { method: 'POST' }); await openRun(run.id); }
    catch (error) { notice('#runError', error.message); }
    finally { state.busy = false; $('#retryRun').disabled = false; readiness(); restoreFocus(); }
  }
  function newInvestigation() {
    stopStream(); state.run = null; state.lastEvent = null; state.pending = null; sessionStorage.removeItem('opsgraph.selectedRun'); $('#runWorkspace').hidden = true; $('#onboarding').hidden = false;
    $('#investigationQuestion').value = ''; notice('#composerError'); showView('investigations'); readiness(); renderHistory();
    if (!state.authenticated) openDrawer('credentialDrawer', $('#newInvestigation')); else $('#investigationQuestion').focus();
  }

  async function saveSkill(event) {
    const restoreFocus = guardAsyncFocus($('#saveSkill'), $('#skillStatus'));
    event.preventDefault(); notice('#skillError'); $('#saveSkill').disabled = true; $('#publishSkill').disabled = true; state.savedSkill = null;
    try { const definition = JSON.parse($('#skillJson').value); await api('/api/skills/drafts', { method: 'POST', body: JSON.stringify(definition) }); state.savedSkill = definition.id; $('#publishSkill').disabled = false; $('#skillStatus').textContent = 'Validated draft saved. Review its scope before publishing.'; }
    catch (error) { notice('#skillError', error.message); $('#skillStatus').textContent = 'Draft was not saved.'; }
    finally { $('#saveSkill').disabled = false; restoreFocus(); }
  }
  async function publishSkill() {
    if (!state.savedSkill) return;
    const restoreFocus = guardAsyncFocus($('#publishSkill'), $('#skillStatus'));
    $('#publishSkill').disabled = true; notice('#skillError');
    try { await api(`/api/skills/${encodeURIComponent(state.savedSkill)}/publish`, { method: 'POST' }); await loadSkills(); $('#skillStatus').textContent = 'Playbook published and available for selection.'; state.savedSkill = null; }
    catch (error) { notice('#skillError', error.message); $('#publishSkill').disabled = false; }
    finally { restoreFocus(); }
  }
  document.addEventListener('click', event => {
    const view = event.target.closest('[data-view]'); if (view) showView(view.dataset.view);
    const run = event.target.closest('[data-run-id]'); if (run) openRun(run.dataset.runId);
    const source = event.target.closest('[data-edit-source]'); if (source) sourceSetup(state.sources.find(item => item.id === source.dataset.editSource));
    const finding = event.target.closest('[data-finding]'); if (finding) showFinding(Number(finding.dataset.finding), finding);
    const evidence = event.target.closest('[data-evidence]'); if (evidence) {
      const returnTarget = state.activeDrawer ? state.returnFocus : evidence;
      showEvidence(Number(evidence.dataset.evidence), returnTarget);
    }
    if (event.target.closest('.drawer-close')) closeDrawer();
  });
  document.addEventListener('keydown', event => {
    if (!state.activeDrawer) return;
    if (event.key === 'Escape') { event.preventDefault(); closeDrawer(); return; }
    if (event.key !== 'Tab') return;
    const focusable = $$('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary, [tabindex="0"]', state.activeDrawer).filter(node => node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  $('.skip-link').addEventListener('click', event => { event.preventDefault(); showView('investigations', false); $('#workspace').focus(); });
  $('#drawerBackdrop').addEventListener('click', () => closeDrawer());
  ['#openCredential', '#setupCredential'].forEach(id => $(id).addEventListener('click', event => openDrawer('credentialDrawer', event.currentTarget)));
  $('#credentialForm').addEventListener('submit', connectWorkspace); $('#clearCredential').addEventListener('click', clearWorkspace);
  $('#newInvestigation').addEventListener('click', newInvestigation); $('#addSource').addEventListener('click', () => sourceSetup());
  $('#sourceForm').addEventListener('submit', saveSource); $('#investigationForm').addEventListener('submit', submitRun);
  $('#investigationSource').addEventListener('change', renderComposerScope); $('#investigationSkill').addEventListener('change', renderComposerScope);
  $('#testProvider').addEventListener('click', testProvider); $('#cancelRun').addEventListener('click', cancelRun); $('#retryRun').addEventListener('click', retryRun);
  $('#caseSearch').addEventListener('input', renderHistory);
  $('#skillForm').addEventListener('submit', saveSkill); $('#publishSkill').addEventListener('click', publishSkill);
  $('#skillJson').addEventListener('input', () => { state.savedSkill = null; $('#publishSkill').disabled = true; });
  $('#inspectedTables').addEventListener('change', () => { $('#sourceTables').value = $$('[data-inspected-table]:checked').map(input => input.dataset.inspectedTable).join(', '); });
  $('#toggleHistory').addEventListener('click', () => { const hidden = !$('#historyPanel').hidden; $('#historyPanel').hidden = hidden; $('.investigation-layout').classList.toggle('history-hidden', hidden); $('#toggleHistory').setAttribute('aria-expanded', String(!hidden)); $('#toggleHistory').textContent = hidden ? 'Show history' : 'Hide history'; });
  $('#toggleEvidence').addEventListener('click', () => { $('#evidencePanel').hidden = !$('#evidencePanel').hidden; $('#toggleEvidence').setAttribute('aria-expanded', String(!$('#evidencePanel').hidden)); });
  $('#exportEvidence').addEventListener('click', async () => { if (!state.run) return; try { download(`${state.run.id}.json`, await api(`/api/runs/${encodeURIComponent(state.run.id)}/export`)); } catch (error) { notice('#runError', error.message); } });
  $('#exportAudit').addEventListener('click', async () => { try { download('opsgraph-audit.json', await api('/api/audit')); } catch (error) { notice('#globalError', error.message); } });
  window.addEventListener('beforeunload', stopStream);
  async function startWorkspace() {
    const epoch = state.authEpoch;
    if (launchToken !== null) {
      try {
        const response = await fetch('/api/launcher/session', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ token: launchToken }), cache: 'no-store' });
        const result = await response.json();
        if (epoch !== state.authEpoch) return;
        if (!response.ok || !validWorkspaceKey(result.key)) throw new Error('Local connection expired. Relaunch OpsGraph or connect using your workspace key.');
        sessionStorage.setItem('opsgraph.workspaceKey', result.key);
      } catch (error) { if (epoch !== state.authEpoch) return; notice('#globalError', error.message); }
    }
    if (epoch !== state.authEpoch) return;
    if (key()) {
      try { await loadWorkspace(); if (epoch !== state.authEpoch) return; const selected = sessionStorage.getItem('opsgraph.selectedRun'); if (selected) await openRun(selected); }
      catch (error) { if (epoch !== state.authEpoch) return; notice('#globalError', error.message); readiness(); }
    }
  }
  readiness(); loadBootstrap(); startWorkspace();
})();
