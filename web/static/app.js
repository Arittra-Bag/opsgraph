(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);

  const cases = {
    checkout: {
      id: 'INV-SAMPLE-001',
      title: 'Webhook failures after deployment',
      subtitle: 'Investigate webhook failures after deploy-1842.',
      state: 'COMPLETE',
    },
    queue: {
      id: 'INV-0241',
      title: 'Queue latency spike',
      subtitle: 'Identify which producer or worker change preceded queue degradation.',
      state: 'REVIEWING',
    },
    billing: {
      id: 'INV-0234',
      title: 'Billing aggregation anomaly',
      subtitle: 'Investigate conflicting totals without exposing customer identities.',
      state: 'POLICY BLOCKED',
    },
  };

  let activeDrawer = null;
  let drawerReturnFocus = null;
  let replaying = false;
  let toastTimer;
  let activeRunMode = 'sample';
  let lastInvestigation = null;
  let lastSavedSkillId = null;
  let sourcesById = new Map();
  let activeCaseFilter = 'all';

  function showToast(message) {
    const toast = $('#toast');
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
  }

  function showView(name) {
    $$('[data-view-panel]').forEach(panel => {
      const visible = panel.dataset.viewPanel === name;
      panel.hidden = !visible;
      panel.classList.toggle('active', visible);
    });
    $$('.nav-item').forEach(button => {
      const active = button.dataset.view === name;
      button.classList.toggle('active', active);
      active ? button.setAttribute('aria-current', 'page') : button.removeAttribute('aria-current');
    });
    const heading = $(`[data-view-panel="${name}"] h1`);
    if (heading) {
      heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
    }
  }

  $$('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));

  $$('.case-card').forEach(button => button.addEventListener('click', () => {
    const data = cases[button.dataset.case];
    $$('.case-card').forEach(card => card.classList.toggle('active', card === button));
    $('#caseTitle').textContent = data.title;
    $('#caseSubtitle').textContent = data.subtitle;
    $('.breadcrumb span:first-child').textContent = data.id;
    $('.inspector-head small').textContent = data.id;
    $('#runState').className = 'run-state complete';
    $('#runState').innerHTML = `<i></i>${data.state}`;
  }));

  function applyCaseFilters() {
    const query = $('#caseSearch').value.trim().toLowerCase();
    $$('.case-card').forEach(card => {
      const state = card.dataset.filterState || (card.dataset.case === 'checkout' ? 'complete' : 'open');
      const matchesState = activeCaseFilter === 'all' || state === activeCaseFilter;
      const matchesQuery = !query || card.textContent.toLowerCase().includes(query);
      card.hidden = !(matchesState && matchesQuery);
    });
  }

  $('#toggleCaseSearch').addEventListener('click', () => {
    const wrap = $('#caseSearchWrap');
    const expanded = wrap.hidden;
    wrap.hidden = !expanded;
    $('#toggleCaseSearch').setAttribute('aria-expanded', String(expanded));
    if (expanded) $('#caseSearch').focus();
    else { $('#caseSearch').value = ''; applyCaseFilters(); }
  });
  $('#caseSearch').addEventListener('input', applyCaseFilters);

  function activateTab(tab) {
    const tablist = tab.closest('[role="tablist"]');
    $$('[role="tab"]', tablist).forEach(item => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
    });
    $$('.tab-panel', $('.inspector')).forEach(panel => {
      const active = panel.dataset.panel === tab.dataset.tab;
      panel.hidden = !active;
      panel.classList.toggle('active', active);
    });
  }

  $$('.tabs [role="tab"]').forEach(tab => {
    tab.addEventListener('click', () => activateTab(tab));
    tab.addEventListener('keydown', event => {
      const tabs = $$('.tabs [role="tab"]');
      const current = tabs.indexOf(tab);
      let next = null;
      if (event.key === 'ArrowRight') next = tabs[(current + 1) % tabs.length];
      if (event.key === 'ArrowLeft') next = tabs[(current - 1 + tabs.length) % tabs.length];
      if (event.key === 'Home') next = tabs[0];
      if (event.key === 'End') next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      activateTab(next);
      next.focus();
    });
  });

  function drawerFocusables(drawer) {
    return $$('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])', drawer);
  }

  function openDrawer(id, trigger) {
    const drawer = document.getElementById(id);
    if (!drawer) return;
    if (activeDrawer) closeDrawer(false);
    drawerReturnFocus = trigger || document.activeElement;
    activeDrawer = drawer;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    $('#drawerBackdrop').hidden = false;
    document.body.dataset.drawerOpen = 'true';
    drawer.querySelector('.drawer-close')?.focus();
  }

  function closeDrawer(restoreFocus = true) {
    if (!activeDrawer) return;
    activeDrawer.classList.remove('open');
    activeDrawer.setAttribute('aria-hidden', 'true');
    $('#drawerBackdrop').hidden = true;
    delete document.body.dataset.drawerOpen;
    const returnTarget = drawerReturnFocus;
    activeDrawer = null;
    drawerReturnFocus = null;
    if (restoreFocus && returnTarget instanceof HTMLElement) returnTarget.focus();
  }

  function downloadJson(filename, value) {
    const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function showEvidenceDetail(item, trigger) {
    const drawer = $('#evidenceDrawer');
    $('.eyebrow', drawer).textContent = item.id || item.evidence_hash?.slice(0, 20) || 'Evidence';
    $('h2', drawer).textContent = item.purpose || item.excerpt || 'Bounded evidence';
    $('.drawer-body', drawer).innerHTML = `<span class="classification supported">RECORDED</span>
      <p class="lead">Evidence returned from a policy-bounded read-only query.</p>
      <dl class="detail-list"><div><dt>Digest</dt><dd>${esc(item.evidence_hash || item.digest || 'sample')}</dd></div>
      <div><dt>Rows</dt><dd>${esc(item.rows?.length ?? 'sample')}</dd></div>
      <div><dt>Scope</dt><dd>${esc(item.truncated ? 'bounded and truncated' : 'bounded')}</dd></div></dl>
      <pre><code>${esc(JSON.stringify(item.rows || item, null, 2))}</code></pre>`;
    openDrawer('evidenceDrawer', trigger);
  }

  function showQueryDetail(query, trigger) {
    const drawer = $('#queryDrawer');
    $('.eyebrow', drawer).textContent = 'Policy-checked query';
    $('h2', drawer).textContent = query.purpose || 'Bounded SELECT query';
    $('.drawer-body', drawer).innerHTML = `<div class="query-safety"><span>READ ONLY</span><span>POLICY CHECKED</span></div>
      <pre><code>${esc(query.sql || query)}</code></pre>`;
    openDrawer('queryDrawer', trigger);
  }

  function workspaceKey() {
    return sessionStorage.getItem('opsgraph.workspaceKey') || '';
  }

  async function api(path, options = {}) {
    if (!workspaceKey()) throw new Error('Connect the local workspace first.');
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: 'application/json',
        'X-OpsGraph-Key': workspaceKey(),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status}).`);
    return payload;
  }

  function renderCredentialStatus() {
    $('#credentialStatus').textContent = workspaceKey() ? 'Session connected' : 'API key required';
    $('#openCredential').classList.toggle('connected', Boolean(workspaceKey()));
  }

  function setTrustSignal(selector, label, safe) {
    const signal = $(selector);
    signal.classList.remove('checking');
    signal.classList.toggle('failed', !safe);
    signal.innerHTML = `<span></span>${esc(label)}`;
  }

  async function loadRuntimeTrust() {
    try {
      const response = await fetch('/api/bootstrap', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`Bootstrap failed (${response.status}).`);
      const runtime = await response.json();
      const trust = runtime.trust;
      const egressOff = trust.egress === false;

      setTrustSignal('#trustDeployment', String(trust.deployment).toUpperCase(), true);
      setTrustSignal('#trustAccess', String(trust.access).toUpperCase(), trust.access === 'read-only');
      setTrustSignal('#trustModel', `${String(trust.model).toUpperCase()} CONFIGURED`, true);
      setTrustSignal('#trustEgress', egressOff ? 'EGRESS OFF' : 'EGRESS ON', egressOff);

      $('#runtimeHealth').textContent = 'Runtime configuration loaded';
      $('#runtimeSummary').textContent = egressOff
        ? 'Sample mode is deterministic and makes no model or database call.'
        : 'External egress is enabled by the local operator.';
      $('#runtimeDeployment').textContent = trust.deployment;
      $('#runtimeModel').textContent = `${trust.model} (configured; unused by sample)`;
      $('#runtimeEgress').textContent = egressOff ? 'off' : 'on';
      $('#runtimePolicy').textContent = trust.policy;
      $('#runtimeStatus').innerHTML = '<span>✓</span> Server configuration loaded';
      $('#runtimeProviderSelect').innerHTML = `<option>${esc(trust.model)}</option>`;
      $('#egressSetting').checked = !egressOff;
      $('#egressSettingCopy').textContent = egressOff
        ? 'Disabled by the running server.'
        : 'Enabled by the running server.';
    } catch (error) {
      ['#trustDeployment', '#trustAccess', '#trustModel', '#trustEgress'].forEach(selector => {
        setTrustSignal(selector, 'UNVERIFIED', false);
      });
      $('#runtimeHealth').textContent = 'Runtime unverified';
      $('#runtimeSummary').textContent = 'The browser could not read the server trust configuration.';
      $('#runtimeStatus').innerHTML = '<span>!</span> Runtime configuration unavailable';
      showToast(error.message || 'Could not verify the runtime configuration.');
    }
  }

  async function loadProviderTrust() {
    if (!workspaceKey()) return;
    try {
      const provider = await api('/api/providers/current');
      const health = provider.health || {};
      const available = health.status === 'ready';
      setTrustSignal('#trustModel', available ? 'MODEL READY' : 'MODEL UNAVAILABLE', available);
      $('#runtimeModel').textContent = available
        ? `${health.provider || 'provider'} (${health.model || 'configured'}; ready)`
        : `${health.provider || 'provider'} (${health.detail || 'unavailable'})`;
    } catch (error) {
      setTrustSignal('#trustModel', 'MODEL UNVERIFIED', false);
      $('#runtimeModel').textContent = 'Provider health could not be verified.';
    }
  }

  async function saveCredential() {
    const input = $('#workspaceKey');
    const key = input.value.trim();
    if (!key) {
      showToast('Enter the local workspace API key.');
      input.focus();
      return;
    }
    const button = $('#saveCredential');
    button.disabled = true;
    button.textContent = 'Checking local workspace…';
    try {
      const response = await fetch('/api/sources', { headers: { 'X-OpsGraph-Key': key } });
      if (!response.ok) throw new Error(response.status === 401 ? 'Workspace key was rejected.' : `Workspace check failed (${response.status}).`);
      sessionStorage.setItem('opsgraph.workspaceKey', key);
      input.value = '';
      renderCredentialStatus();
      await Promise.all([loadSources(), loadSkills(), loadProviderTrust()]);
      closeDrawer();
      showToast('Local workspace connected for this tab.');
    } catch (error) {
      showToast(error.message || 'Could not connect to the local workspace.');
      input.focus();
    } finally {
      button.disabled = false;
      button.textContent = 'Connect workspace';
    }
  }

  async function loadSources() {
    if (!workspaceKey()) return;
    const sources = await api('/api/sources');
    sourcesById = new Map(sources.map(source => [source.id, source]));
    const select = $('#investigationSource');
    select.innerHTML = sources
      .filter(source => source.kind === 'postgresql' && source.status === 'ready')
      .map(source => `<option value="${esc(source.id)}">${esc(source.name)}</option>`)
      .join('');
    const catalog = $('#connectedSourceCatalog');
    catalog.innerHTML = sources.filter(source => source.kind === 'postgresql').map(source => `<article class="source-card">
      <div class="source-icon">PG</div><div><span class="source-status"><i></i>${esc(source.status.toUpperCase())}</span>
      <h2>${esc(source.name)}</h2><p>${esc((source.allowed_tables || []).join(', ') || 'No table scope configured')}</p></div>
      <dl><div><dt>Mode</dt><dd>read-only</dd></div><div><dt>Egress</dt><dd>${source.allow_external_egress ? 'approved' : 'local only'}</dd></div></dl>
      <button class="inspect-connected-source" data-source-id="${esc(source.id)}">View configured scope →</button></article>`).join('');
    $$('.inspect-connected-source', catalog).forEach(button => button.addEventListener('click', () => {
      const source = sourcesById.get(button.dataset.sourceId);
      if (!source) return showToast('Source details are unavailable.');
      const drawer = $('#runtimeDrawer');
      $('#runtimeDrawerTitle').textContent = source.name;
      $('.drawer-body', drawer).innerHTML = `<p class="lead">Configured scope for this read-only source.</p><dl class="detail-list"><div><dt>Status</dt><dd>${esc(source.status)}</dd></div><div><dt>Allowed schemas</dt><dd>${esc((source.allowed_schemas || []).join(', ') || 'none')}</dd></div><div><dt>Allowed tables</dt><dd>${esc((source.allowed_tables || []).join(', ') || 'none')}</dd></div><div><dt>External egress</dt><dd>${source.allow_external_egress ? 'approved' : 'local only'}</dd></div></dl>`;
      openDrawer('runtimeDrawer', button);
    }));
  }

  async function loadSkills() {
    if (!workspaceKey()) return;
    const skills = await api('/api/skills');
    $('#skillCatalog').innerHTML = skills.map(skill => `<article>
      <span>${esc(skill.id.toUpperCase())}</span><h2>${esc(skill.name)}</h2>
      <p>Version ${esc(skill.version)} · validated policy-bounded tool configuration.</p>
      <footer><b>${skill.tools.length} tools</b><small>read-only</small></footer>
    </article>`).join('') || '<article><h2>No published skills</h2></article>';
    $('#investigationSkill').innerHTML = `<option value="">Auto-route from question</option>${skills.map(skill =>
      `<option value="${esc(skill.id)}">${esc(skill.name)} · ${esc(skill.version)}</option>`
    ).join('')}`;
  }

  async function saveSource(event) {
    event.preventDefault();
    const sourceId = $('#sourceId').value.trim();
    const schemas = $('#sourceSchemas').value.split(',').map(value => value.trim()).filter(Boolean);
    try {
      const evidenceBindings = $('#sourceEvidenceBindings').value.split(/\n|;/).map(line => line.trim()).filter(Boolean).map(line => {
        const [evidenceType, tables] = line.split('=', 2).map(value => value.trim());
        if (!evidenceType || !tables) throw new Error('Evidence bindings use evidence_type=public.table[, public.table].');
        return { evidence_type: evidenceType, source_tables: tables.split(',').map(value => value.trim()).filter(Boolean) };
      });
      await api('/api/sources', {
        method: 'POST',
        body: JSON.stringify({
          id: sourceId,
          name: $('#sourceName').value.trim(),
          secret_ref: $('#sourceSecretRef').value.trim(),
          allowed_schemas: schemas,
          allowed_tables: $('#sourceTables').value.split(',').map(value => value.trim()).filter(Boolean),
          evidence_bindings: evidenceBindings,
          allow_external_egress: $('#sourceExternalEgress').checked,
        }),
      });
      showToast('Source metadata saved. Verifying read-only access and discovering schema…');
      const snapshot = await api(`/api/sources/${encodeURIComponent(sourceId)}/inspect`, { method: 'POST' });
      await loadSources();
      showToast(`Source ready: ${snapshot.tables.length} tables discovered.`);
    } catch (error) {
      showToast(error.message || 'Source setup failed closed.');
    }
  }

  function renderConnected(result) {
    activeRunMode = 'connected';
    lastInvestigation = result;
    $('#caseTitle').textContent = result.question;
    $('#caseSubtitle').textContent = result.answer.summary;
    $('.breadcrumb span:first-child').textContent = result.id;
    $('.inspector-head small').textContent = result.id;
    $('.scope-strip > div:first-child b').textContent = result.source_id;
    $('.scope-strip > div:nth-child(3) b').textContent = result.skill_id;
    $('#findingGrid').innerHTML = result.answer.findings.map(finding => `<article class="finding">
      <div class="finding-head"><span class="classification ${esc(finding.classification)}">${esc(finding.classification.toUpperCase())}</span><small>${finding.evidence_ids.length} citations</small></div>
      <h3>${esc(finding.claim)}</h3></article>`).join('');
    $('#conclusionTitle').textContent = result.answer.summary;
    $('#conclusionText').textContent = result.answer.limitations.join(' · ') || 'No additional limitation recorded.';
    $('#classificationCount').textContent = String(result.answer.findings.length);
    $('#evidenceCount').textContent = `${result.evidence.length} items`;
    $('#limitationCount').textContent = `${result.answer.limitations.length} recorded`;
    $('#tab-evidence span').textContent = String(result.evidence.length);
    $('#tab-queries span').textContent = String(result.plan.queries.length);
    $('#evidenceLedger').innerHTML = result.evidence.map((item, index) => `<button class="ledger-item connected-evidence" data-index="${index}">
      <span>${esc(item.evidence_hash.slice(0, 20))}…</span><b>${esc(item.purpose)}</b>
      <small>${item.rows.length} bounded rows · ${esc(item.truncated ? 'truncated' : 'complete')}</small></button>`).join('');
    $('#queryLedger').innerHTML = result.plan.queries.map((query, index) => `<button class="query-item connected-query" data-index="${index}">
      <span>Q-${String(index + 1).padStart(2, '0')} · policy checked</span>
      <b>${esc(query.purpose)}</b><small>${esc(query.sql)}</small></button>`).join('');
    $('#runState').className = 'run-state complete';
    $('#runState').innerHTML = '<i></i>COMPLETE';
    $('#replayRun').innerHTML = '<span>⊘</span> Connected run preserved';
    $$('.connected-evidence').forEach(button => button.addEventListener('click', () => {
      showEvidenceDetail(result.evidence[Number(button.dataset.index)], button);
    }));
    $$('.connected-query').forEach(button => button.addEventListener('click', () => {
      showQueryDetail(result.plan.queries[Number(button.dataset.index)], button);
    }));
  }

  async function runConnected(event) {
    event.preventDefault();
    const sourceId = $('#investigationSource').value;
    if (!sourceId) return showToast('Inspect a PostgreSQL source first.');
    try {
      const result = await api('/api/investigations', {
        method: 'POST',
        body: JSON.stringify({ source_id: sourceId, skill_id: $('#investigationSkill').value || null, question: $('#investigationQuestion').value.trim() }),
      });
      renderConnected(result);
      closeDrawer();
      showView('investigations');
      showToast('Connected investigation completed with cited evidence.');
    } catch (error) {
      showToast(error.message || 'Investigation failed closed.');
    }
  }

  async function saveSkill(event) {
    event.preventDefault();
    try {
      const definition = JSON.parse($('#skillJson').value);
      await api('/api/skills/drafts', { method: 'POST', body: JSON.stringify(definition) });
      lastSavedSkillId = definition.id;
      $('#publishSkill').disabled = false;
      showToast('Validated skill draft saved. Publish when the scope is ready.');
    } catch (error) {
      showToast(error.message || 'Skill configuration is invalid.');
    }
  }

  function renderInvestigation(result) {
    activeRunMode = 'sample';
    lastInvestigation = result;
    $('#caseTitle').textContent = result.title;
    $('#caseSubtitle').textContent = result.summary;
    $('.breadcrumb span:first-child').textContent = result.id;
    $('.inspector-head small').textContent = result.id;
    $('.scope-strip > div:first-child b').textContent = result.source;
    $('.scope-strip > div:nth-child(3) b').textContent = result.playbook;

    const knownClasses = new Set(['supported', 'possible', 'unknown', 'contradictory']);
    $('#findingGrid').innerHTML = result.findings.map((finding, index) => {
      const classification = knownClasses.has(finding.classification) ? finding.classification : 'unknown';
      const limitation = finding.limitation ? `<p>${esc(finding.limitation)}</p>` : '';
      return `<article class="finding${index === 0 ? ' primary-finding' : ''}">
        <div class="finding-head"><span class="classification ${classification}">${esc(classification.toUpperCase())}</span><small>${finding.evidence_ids.length} citation${finding.evidence_ids.length === 1 ? '' : 's'}</small></div>
        <h3>${esc(finding.statement)}</h3>${limitation}
      </article>`;
    }).join('');

    $('#conclusionTitle').textContent = result.summary;
    $('#conclusionText').textContent = 'Read-only next checks: inspect the cited evidence and recorded limitations. No production action was proposed or executed.';
    $('#classificationCount').textContent = String(result.findings.length);
    $('#evidenceCount').textContent = `${result.evidence.length} items`;
    $('#limitationCount').textContent = `${result.limitations.length} recorded`;
    $('#tab-evidence span').textContent = String(result.evidence.length);
    $('#tab-queries span').textContent = String(result.queries.length);

    $('#evidenceLedger').innerHTML = result.evidence.map(item => `<button class="ledger-item result-evidence">
      <span>${esc(item.id)}</span><b>${esc(item.excerpt)}</b><small>${esc(item.source)} · digest ${esc(item.digest.slice(0, 12))}…</small>
    </button>`).join('');
    $$('.result-evidence').forEach((button, index) => button.addEventListener('click', () => showEvidenceDetail(result.evidence[index], button)));

    $('#queryLedger').innerHTML = result.queries.map((query, index) => `<button class="query-item result-query">
      <span>Q-${String(index + 1).padStart(2, '0')} · bounded</span><b>${esc(query)}</b><small>SELECT · synthetic sample · policy checked</small>
    </button>`).join('');
    $$('.result-query').forEach((button, index) => button.addEventListener('click', () => showQueryDetail({ purpose: `Sample query Q-${index + 1}`, sql: result.queries[index] }, button)));

    const nodes = $$('.path-node');
    result.trace.forEach((step, index) => {
      if (!nodes[index]) return;
      $('b', nodes[index]).textContent = step.label;
      $('small', nodes[index]).textContent = step.detail;
    });
  }

  $$('[data-drawer]').forEach(button => button.addEventListener('click', () => openDrawer(button.dataset.drawer, button)));
  $$('.drawer-close').forEach(button => button.addEventListener('click', () => closeDrawer()));
  $('#drawerBackdrop').addEventListener('click', () => closeDrawer());
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && activeDrawer) closeDrawer();
    if (event.key !== 'Tab' || !activeDrawer) return;
    const focusables = drawerFocusables(activeDrawer);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  async function replay() {
    if (replaying) return;
    if (!workspaceKey()) {
      openDrawer('credentialDrawer', $('#replayRun'));
      setTimeout(() => $('#workspaceKey').focus(), 0);
      showToast('Connect the local workspace to run the sample.');
      return;
    }
    if (activeRunMode === 'connected') {
      showToast('Connected evidence is preserved. Start a new run instead of replacing it with a sample replay.');
      return;
    }
    replaying = true;
    const button = $('#replayRun');
    const nodes = $$('.path-node');
    button.disabled = true;
    button.innerHTML = '<span>■</span> Replaying…';
    $('#runState').className = 'run-state running';
    $('#runState').innerHTML = '<i></i>REPLAYING';
    nodes.forEach(node => node.classList.remove('done', 'active'));

    try {
      const response = await fetch('/api/investigations/sample', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-OpsGraph-Key': workspaceKey(),
        },
        body: JSON.stringify({ question: $('#caseSubtitle').textContent.trim() }),
      });
      if (!response.ok) {
        if (response.status === 401) {
          sessionStorage.removeItem('opsgraph.workspaceKey');
          renderCredentialStatus();
          openDrawer('credentialDrawer', button);
        }
        throw new Error(response.status === 401 ? 'Workspace key was rejected.' : `Replay failed (${response.status}).`);
      }
      const result = await response.json();
      renderInvestigation(result);

      for (const node of nodes) {
        node.classList.add('active');
        await new Promise(resolve => setTimeout(resolve, reduceMotion ? 20 : 420));
        node.classList.remove('active');
        node.classList.add('done');
      }

      $('#runState').className = 'run-state complete';
      $('#runState').innerHTML = '<i></i>COMPLETE';
      showToast('Sample replay completed and evidence classifications were refreshed.');
    } catch (error) {
      $('#runState').className = 'run-state denied';
      $('#runState').innerHTML = '<i></i>NOT RUN';
      showToast(error.message || 'The sample replay could not be completed.');
    } finally {
      button.disabled = false;
      button.innerHTML = '<span>▶</span> Replay investigation';
      replaying = false;
    }
  }

  $('#replayRun').addEventListener('click', replay);
  $('#saveCredential').addEventListener('click', saveCredential);
  $('#workspaceKey').addEventListener('keydown', event => { if (event.key === 'Enter') saveCredential(); });
  $('#clearCredential').addEventListener('click', () => {
    sessionStorage.removeItem('opsgraph.workspaceKey');
    $('#workspaceKey').value = '';
    renderCredentialStatus();
    showToast('Session workspace key forgotten.');
  });
  $('#newInvestigation').addEventListener('click', async () => {
    if (!workspaceKey()) return openDrawer('credentialDrawer', $('#newInvestigation'));
    try { await loadSources(); } catch (error) { return showToast(error.message); }
    openDrawer('investigationDrawer', $('#newInvestigation'));
  });

  function toggleSourceSetup() {
    const setup = $('#sourceSetup');
    setup.hidden = !setup.hidden;
    if (!setup.hidden) {
      setup.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' });
      $('#sourceId').focus({ preventScroll: true });
    }
  }
  $('#addSource').addEventListener('click', toggleSourceSetup);
  $('#addSourceCard').addEventListener('click', toggleSourceSetup);
  $('#sourceForm').addEventListener('submit', saveSource);
  $('#investigationForm').addEventListener('submit', runConnected);
  $('#skillForm').addEventListener('submit', saveSkill);
  $('#publishSkill').addEventListener('click', async () => {
    if (!lastSavedSkillId) return showToast('Save a skill draft before publishing it.');
    try {
      await api(`/api/skills/${encodeURIComponent(lastSavedSkillId)}/publish`, { method: 'POST' });
      await loadSkills();
      $('#publishSkill').disabled = true;
      showToast('Skill published and available for explicit selection.');
    } catch (error) {
      showToast(error.message || 'Skill publish failed closed.');
    }
  });
  $('#exportEvidence').addEventListener('click', () => {
    if (!lastInvestigation) return showToast('Run an investigation before exporting evidence.');
    downloadJson(`${lastInvestigation.id || 'opsgraph'}-evidence.json`, {
      id: lastInvestigation.id,
      source: lastInvestigation.source_id || lastInvestigation.source,
      evidence: lastInvestigation.evidence,
    });
    showToast('Evidence export downloaded locally.');
  });
  $('#exportAudit').addEventListener('click', async () => {
    try {
      downloadJson('opsgraph-audit.json', await api('/api/audit'));
      showToast('Audit export downloaded locally.');
    } catch (error) { showToast(error.message || 'Audit export failed.'); }
  });
  $('#viewPolicy').addEventListener('click', async event => {
    try {
      const policy = await api('/api/policies/current');
      $('#denialDrawer .eyebrow').textContent = 'Effective server policy';
      $('#denialDrawer h2').textContent = policy.id;
      $('#denialDrawer .drawer-body').innerHTML = `<pre><code>${esc(JSON.stringify(policy, null, 2))}</code></pre>`;
      openDrawer('denialDrawer', event.currentTarget);
    } catch (error) { showToast(error.message || 'Policy could not be loaded.'); }
  });
  $('#inspectSample').addEventListener('click', event => {
    const drawer = $('#runtimeDrawer');
    $('#runtimeDrawerTitle').textContent = 'Fictional SaaS sample';
    $('.drawer-body', drawer).innerHTML = '<p class="lead">Built-in synthetic dataset for safe offline replay.</p><dl class="detail-list"><div><dt>Tables</dt><dd>6</dd></div><div><dt>Relations</dt><dd>9</dd></div><div><dt>Dataset hash</dt><dd>8c91…d42a</dd></div><div><dt>Database access</dt><dd>None</dd></div></dl>';
    openDrawer('runtimeDrawer', event.currentTarget);
  });
  $('#reviewSourceContract').addEventListener('click', event => {
    const drawer = $('#runtimeDrawer');
    $('#runtimeDrawerTitle').textContent = 'PostgreSQL source contract';
    $('.drawer-body', drawer).innerHTML = '<p class="lead">OpsGraph discovers approved schema metadata, then runs only policy-bounded SELECT queries against the configured table scope.</p><dl class="detail-list"><div><dt>Writes</dt><dd>Blocked</dd></div><div><dt>Scope</dt><dd>Schema and table allowlist</dd></div><div><dt>Rows</dt><dd>Bounded per policy</dd></div><div><dt>Egress</dt><dd>Off by default; explicit source opt-in required</dd></div></dl>';
    openDrawer('runtimeDrawer', event.currentTarget);
  });

  $$('.case-filters button').forEach(button => button.addEventListener('click', () => {
    $$('.case-filters button').forEach(item => item.classList.toggle('active', item === button));
    activeCaseFilter = button.textContent.trim().toLowerCase();
    applyCaseFilters();
  }));

  $$('.path-node').forEach(node => node.addEventListener('click', () => {
    const target = node.dataset.step === 'collect' ? 'queries' : node.dataset.step === 'report' ? 'audit' : 'plan';
    const tab = $(`[data-tab="${target}"]`);
    activateTab(tab);
    tab.focus();
  }));

  renderCredentialStatus();
  loadRuntimeTrust();
  if (workspaceKey()) Promise.all([loadSources(), loadSkills(), loadProviderTrust()]).catch(error => showToast(error.message));
})();
