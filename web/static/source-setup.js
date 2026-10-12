/* One setup flow over the existing source operations. Secrets never enter session state. */
(function (root) {
  'use strict';
  const steps = ['Database host', 'Private connection', 'Choose tables', 'Inspect and review', 'Check and finish'];
  const brands = { local: 'postgresql', self_hosted: 'postgresql', supabase: 'supabase', neon: 'neon', aws_rds: 'aws_rds', google_cloud_sql: 'google_cloud_sql', azure: 'azure', digitalocean: 'digitalocean' };
  const views = new Set(['investigations', 'sources', 'work-outcomes', 'playbooks', 'policies', 'audit', 'settings']);
  const storageName = 'opsgraph.navigation';
  function readSession(storage) {
    try {
      const value = JSON.parse(storage.getItem(storageName) || '{}');
      return {
        view: views.has(value.view) ? value.view : 'investigations',
        sourceId: typeof value.sourceId === 'string' && /^[a-z][a-z0-9-]{1,63}$/.test(value.sourceId) ? value.sourceId : null,
      };
    } catch { return { view: 'investigations', sourceId: null }; }
  }
  function saveSession(storage, update) {
    try { storage.setItem(storageName, JSON.stringify(readSession({ getItem: () => JSON.stringify({ ...readSession(storage), ...update }) }))); } catch { /* Storage can be disabled; saved backend checks still work. */ }
  }
  function icon(profile) {
    return `<img class="provider-icon" src="/assets/static/provider-icons/${Object.hasOwn(brands, profile) ? brands[profile] : 'postgresql'}.svg" width="32" height="32" alt="" aria-hidden="true">`;
  }
  function verifiedURI(value) {
    if (!/^postgres(?:ql)?:\/\//i.test(value)) return value;
    try {
      const url = new URL(value);
      if (!['127.0.0.1', 'localhost', '[::1]', '::1'].includes(url.hostname)) url.searchParams.set('sslmode', 'verify-full');
      return url.href;
    } catch { return value; } // The shared backend validator provides a redacted error.
  }
  function sourceLabel(source, checkPassed = false) {
    if (source?.status === 'stale' || source?.readiness?.status === 'stale') return 'Review tables again';
    if (source?.readiness?.status === 'failed') return 'Query check failed';
    if (checkPassed) return 'Query check passed';
    return source?.status === 'ready' ? 'Tables inspected' : 'Setup needed';
  }
  class SourceSetup {
    constructor({ document, storage, request, dirty, discovered, source, authenticated, guardFocus }) {
      Object.assign(this, { document, storage, request, dirty, discovered, source, authenticated, guardFocus });
      this.step = 1; this.token = 0; this.connections = []; this.connectionBusy = false;
      this.$ = id => document.getElementById(id);
      this.$('sourceNext').addEventListener('click', () => this.next());
      this.$('sourceBack').addEventListener('click', () => this.go(this.step - 1));
      this.$('sourceConnectionForm').addEventListener('submit', event => { event.preventDefault(); void this.saveConnection(); });
      this.$('existingConnection').addEventListener('change', () => {
        this.$('sourceSecretRef').value = this.$('existingConnection').value;
        this.connectionMode(); this.dirty();
      });
      this.$('sourceDsn').addEventListener('input', () => { this.$('existingConnection').value = ''; this.connectionMode(); });
      this.$('sourceHosting').addEventListener('change', () => this.provider());
      this.$('sourceRefreshHistory').addEventListener('click', () => void this.history());
    }
    icon(profile) { return icon(profile); }
    label(source, checkPassed) { return sourceLabel(source, checkPassed); }
    session() { return readSession(this.storage); }
    rememberView(view) { saveSession(this.storage, { view }); }
    async resumeConnection(reference) {
      this.$('sourceSecretRef').value = reference;
      await this.connectionsFor(reference);
      if (this.$('existingConnection').value === reference && this.authenticated()) {
        const connection = this.connections.find(item => item.secret_ref === reference);
        if (connection?.hosting_profile) { this.$('sourceHosting').value = connection.hosting_profile; this.provider(); }
        this.go(3); await this.discovered();
      }
    }
    message(text, error = false) {
      const node = this.$('connectionSaveStatus'); node.textContent = text; node.classList.toggle('error', error);
    }
    provider() {
      this.$('sourceProviderIcon').innerHTML = icon(this.$('sourceHosting').value);
      this.$('sourceProviderTip').innerHTML = this.$('sourceHosting').value === 'supabase'
        ? '<b>Before you copy the connection</b><ul><li><strong>Network:</strong> IPv4-only? Choose the session pooler on port 5432.</li><li><strong>Login:</strong> use your read-only role plus project ID, as shown in Supabase Connect.</li><li><strong>Certificate:</strong> download the project CA for the next step.</li></ul>'
        : '<b>Before you connect</b><ul><li><strong>Login:</strong> use a role that can only read your chosen tables.</li><li><strong>Remote databases:</strong> prepare a trusted CA and use the certificate’s hostname.</li></ul>';
    }
    connectionMode() {
      const selected = this.$('existingConnection').value;
      this.$('newConnectionFields').hidden = Boolean(selected);
      this.$('sourceDsn').required = !selected;
      this.$('sourceDsn').value = selected ? '' : this.$('sourceDsn').value;
      if (selected) this.$('sourceSecretRef').value = selected;
      this.$('sourceNext').disabled = this.connectionBusy;
    }
    async connectionsFor(reference) {
      const token = this.token;
      const loading = this.connectionRequestToken = (this.connectionRequestToken || 0) + 1;
      try {
        const payload = await this.request('/api/postgres/connections');
        if (token !== this.token || loading !== this.connectionRequestToken || !this.authenticated()) return;
        this.connections = payload.connections || [];
        const select = this.$('existingConnection'); select.replaceChildren();
        const fresh = this.document.createElement('option'); fresh.value = ''; fresh.textContent = 'Add a new private connection'; select.append(fresh);
        this.connections.forEach((connection, index) => {
          const option = this.document.createElement('option'); option.value = connection.secret_ref;
          option.textContent = `Saved connection ${index + 1}${connection.managed ? '' : ' · configured by your operator'}${connection.configured ? '' : ' · needs repair'}`;
          option.disabled = !connection.configured; select.append(option);
        });
        select.value = this.connections.some(item => item.secret_ref === reference && item.configured) ? reference : '';
        this.connectionMode();
      } catch (error) { if (token === this.token && loading === this.connectionRequestToken) this.message(error.message, true); }
    }
    start(value) {
      this.token++; this.connectionBusy = false;
      this.inspectionReady = value?.status === 'ready';
      ['sourceDsn', 'sourceCaFile', 'sourceTlsCa', 'existingConnection'].forEach(id => { this.$(id).disabled = false; });
      this.$('sourceDsn').value = ''; this.$('sourceCaFile').value = '';
      this.$('sourceTlsCa').value = ''; this.message('Passwords are sent only to this backend and are never displayed again.');
      void this.connectionsFor(this.$('sourceSecretRef').value);
      this.provider();
      const resume = value?.status === 'ready' ? (['ready', 'failed'].includes(value.readiness?.status) ? 5 : 4) : 1;
      this.go(resume);
      saveSession(this.storage, { view: 'sources', sourceId: value?.id || null });
      void this.history();
    }
    go(number, focus = true) {
      this.step = Math.min(5, Math.max(1, number));
      this.document.querySelectorAll('[data-source-step]').forEach(panel => { panel.hidden = Number(panel.dataset.sourceStep) !== this.step; });
      this.document.querySelectorAll('[data-source-progress]').forEach(item => {
        const active = Number(item.dataset.sourceProgress) === this.step;
        active ? item.setAttribute('aria-current', 'step') : item.removeAttribute('aria-current');
      });
      const heading = this.$('sourceSetupTitle'); heading.textContent = `Step ${this.step} of 5 · ${steps[this.step - 1]}`;
      this.$('sourceBack').hidden = this.step === 1;
      this.$('sourceNext').hidden = this.step === 5 || (this.step === 4 && !this.inspectionReady);
      this.$('sourceNext').textContent = this.step === 4 ? 'Continue to query check' : this.step === 2 && !this.$('existingConnection').value ? 'Save connection and continue' : 'Continue';
      if (this.step === 4) this.$('sourceReviewSelection').textContent = `Selected tables: ${this.$('sourceTables').value || 'none'}. Inspect the login and columns before approving a read.`;
      this.$('sourceNext').disabled = this.connectionBusy;
      if (focus) { heading.focus({ preventScroll: true }); heading.scrollIntoView?.({ block: 'start' }); }
    }
    async next() {
      if (this.connectionBusy) return;
      if (this.step === 1) { this.go(2); return; }
      if (this.step === 2) {
        if (!this.$('existingConnection').value) { await this.saveConnection(); return; }
        this.$('sourceSecretRef').value = this.$('existingConnection').value;
        this.go(3); await this.discovered(); return;
      }
      if (this.step === 3) {
        const inputs = ['sourceName', 'sourceId', 'sourceSecretRef', 'sourceSchemas', 'sourceTables'];
        if (inputs.some(id => !this.$(id).reportValidity())) return;
        this.go(4);
      }
      else if (this.step === 4 && this.inspectionReady) this.go(5);
    }
    async saveConnection() {
      if (this.connectionBusy || !this.$('sourceDsn').reportValidity()) return;
      const control = this.document.activeElement === this.$('sourceDsn') ? this.$('sourceDsn') : this.$('sourceNext');
      const restoreFocus = this.guardFocus?.(control) || (() => {});
      const token = this.token; this.connectionBusy = true; this.go(2, false);
      ['sourceDsn', 'sourceCaFile', 'sourceTlsCa', 'existingConnection'].forEach(id => { this.$(id).disabled = true; });
      const request = { dsn: verifiedURI(this.$('sourceDsn').value.trim()), hosting_profile: this.$('sourceHosting').value };
      this.$('sourceDsn').value = ''; this.message('Saving the private connection. No database or model request runs here.');
      try {
        const file = this.$('sourceCaFile').files?.[0];
        if (file && file.size > 65_536) throw new Error('Choose a PEM CA file of at most 64 KB.');
        if (file) request.ca_pem = await file.text();
        else if (this.$('sourceTlsCa').value.trim()) {
          // libpq keyword strings are also accepted by the backend; paths with spaces stay intact.
          if (/^postgres(?:ql)?:\/\//i.test(request.dsn)) {
            const url = new URL(request.dsn); url.searchParams.set('sslrootcert', this.$('sourceTlsCa').value.trim()); request.dsn = url.href;
          } else throw new Error('Use a PostgreSQL URI with a CA path, or upload the PEM certificate.');
        }
        if (token !== this.token || !this.authenticated()) return;
        const saved = await this.request('/api/postgres/connections', { method: 'PUT', body: JSON.stringify(request) });
        if (token !== this.token || !this.authenticated()) return;
        this.$('sourceSecretRef').value = saved.secret_ref; this.dirty();
        await this.connectionsFor(saved.secret_ref);
        if (token !== this.token) return;
        this.message('Connection saved privately. Now choose the exact tables it may read.');
        this.go(3); await this.discovered();
      } catch (error) {
        if (token === this.token && this.authenticated()) this.message(`${error.message} Re-enter the connection to retry; no password was kept in this page.`, true);
      } finally {
        request.dsn = ''; request.ca_pem = '';
        if (token === this.token) {
          this.$('sourceCaFile').value = ''; this.connectionBusy = false;
          ['sourceDsn', 'sourceCaFile', 'sourceTlsCa', 'existingConnection'].forEach(id => { this.$(id).disabled = false; });
          this.go(this.step, false); restoreFocus();
        } else restoreFocus(false);
      }
    }
    inspected() {
      const selected = this.source();
      saveSession(this.storage, { view: 'sources', sourceId: selected?.id || this.$('sourceId').value });
      this.inspectionReady = true; this.go(4); void this.history();
    }
    invalidate() { this.inspectionReady = false; this.go(this.step, false); }
    refreshSaved() {
      const selected = this.source();
      if (!selected || this.step !== 5) return;
      const result = selected.readiness;
      this.$('sourceReadinessStatus').textContent = result?.status === 'ready'
        ? `Passed ${new Date(result.checked_at).toLocaleString()} against ${result.table}. No source values returned.`
        : result?.status === 'failed' ? `Last check failed ${new Date(result.checked_at).toLocaleString()}. Correct the connection, then run it again.`
          : 'Review the saved inspection and run a fresh query check.';
    }
    async history() {
      const selected = this.source(); const token = this.token;
      const node = this.$('sourceCheckHistory');
      if (!selected) { node.textContent = 'Checks appear here after this source is saved.'; return; }
      try {
        const response = await this.request(`/api/checks?source_id=${encodeURIComponent(selected.id)}`);
        if (token !== this.token || !this.authenticated()) return;
        node.replaceChildren();
        for (const row of response.history || []) {
          const item = this.document.createElement('li');
          item.textContent = `${row.check} · ${row.status} · ${new Date(row.checked_at).toLocaleString()}`;
          node.append(item);
        }
        if (!response.history?.length) node.textContent = 'No checks recorded for this source yet.';
      } catch (error) { if (token === this.token) node.textContent = error.message; }
    }
    reset() {
      this.token++; this.$('sourceDsn').value = ''; this.$('sourceCaFile').value = '';
      try { this.storage.removeItem(storageName); } catch { /* Browser storage can be disabled. */ }
    }
  }
  function watchWorkspace({ fetch, url, key, authenticated, parser, changed, wait = ms => new Promise(resolve => setTimeout(resolve, ms)) }) {
    let stopped = false, controller = null, revision = 0, refreshing = false, pending = false;
    const stop = () => { stopped = true; controller?.abort(); };
    async function refresh() {
      pending = true;
      if (refreshing) return;
      refreshing = true;
      try { while (pending && !stopped && authenticated()) { pending = false; await changed(); } }
      finally { refreshing = false; }
    }
    async function run() {
      let delay = 500;
      while (!stopped && authenticated()) {
        const credential = key(); controller = new AbortController();
        try {
          const response = await fetch(url(revision), { cache: 'no-store', redirect: 'error', signal: controller.signal, headers: { Accept: 'text/event-stream', 'X-OpsGraph-Key': credential } });
          if (!response.ok || !response.body) { if (response.status === 401 || response.status === 403) return; throw new Error('Metadata stream unavailable'); }
          const decoder = new TextDecoder(); const events = parser(event => {
            if (stopped || !authenticated() || key() !== credential || event.type !== 'workspace_changed') return;
            try { const data = JSON.parse(event.data); if (Number.isSafeInteger(data.revision) && data.revision >= 0) { revision = data.revision; void refresh(); } } catch { /* Ignore malformed notifications; backend reads are authoritative. */ }
          });
          const reader = response.body.getReader();
          try { while (!stopped && authenticated() && key() === credential) { const part = await reader.read(); if (part.done) break; events.push(decoder.decode(part.value, { stream: true })); } }
          finally { await reader.cancel().catch(() => {}); events.finish(); }
          delay = 500;
        } catch (error) { if (stopped || error.name === 'AbortError') return; delay = Math.min(delay * 2, 10_000); }
        if (!stopped && authenticated()) await wait(delay);
      }
    }
    void run(); return { stop };
  }
  const api = { sourceLabel, icon, readSession, saveSession, verifiedURI, SourceSetup, watchWorkspace, mount: options => new SourceSetup(options) };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OpsGraphSourceSetup = api;
})(typeof window !== 'undefined' ? window : globalThis);
