(() => {
  'use strict';
  const panel = document.getElementById('database-tools');
  if (!panel || panel.dataset.enabled !== 'true') return;
  const node = id => document.getElementById(`database-${id}`);
  const base = '/admin/database';
  let operation = null, busy = false, nativeRestart = false, holdOperation = null;
  let job = sessionStorage.getItem('wamf-database-job');
  let operationId = sessionStorage.getItem('wamf-database-operation');
  let restarting = false, restartStart = 0, timer = null;
  let restartInstance = sessionStorage.getItem('wamf-database-restart-instance');
  const message = text => { node('status').textContent = text; };
  const labels = {staging: 'Staging package', validating: 'Checking integrity and compatibility',
    backing_up: 'Copying database', packaging: 'Packaging verified backup', publishing: 'Publishing backup',
    preflight: 'Checking database', ready: 'Backup validated — review the restore preview',
    confirmed: 'Restore confirmed', pending_restart: 'Restore queued — waiting for controlled restart',
    preparing: 'Preparing offline restore', migrating: 'Migrating working copy', rollback_ready: 'Rollback backup preserved',
    replacing: 'Applying offline restore', applying: 'Applying offline restore', replaced: 'Database replaced',
    startup_pending: 'Validating restored database startup', complete: 'Operation complete',
    failed: 'Operation failed', rejected: 'Backup rejected', recovery_required: 'Offline recovery required', cancelled: 'Operation cancelled'};
  async function api(path, options = {}) {
    const response = await fetch(base + path, {credentials: 'same-origin', cache: 'no-store', ...options,
      headers: {'X-CSRF-Token': panel.dataset.csrf, ...(options.headers || {})}});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.message || data.code || 'Database request failed.');
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function saveOperation(id) {
    operationId = id;
    if (id) sessionStorage.setItem('wamf-database-operation', id);
    else sessionStorage.removeItem('wamf-database-operation');
  }
  function controls() {
    node('create').disabled = busy || restarting;
    node('upload').disabled = busy || restarting;
    node('apply').disabled = busy || restarting || !nativeRestart || !node('confirm-check').checked;
    node('ack').disabled = busy || restarting || !node('ack-check').checked;
    panel.querySelectorAll('[data-backup-id]').forEach(button => { button.disabled = busy || restarting; });
  }
  function preview(data) {
    operation = data;
    saveOperation(data.operation_id);
    const plan = data.plan;
    node('preview').hidden = !plan || !['ready', 'confirmed'].includes(data.phase);
    node('recovery').hidden = data.phase !== 'recovery_required';
    if (plan) {
      const detected = plan.detected_migration_id || 'recognized legacy schema';
      node('preview-schema').textContent = `${plan.compatibility}: ${detected}. ${plan.migration_required ? `Migration required to ${plan.latest_migration_id}.` : 'Schema is current.'} WAMF ${plan.wamf_version}.`;
      node('preview-warning').textContent = plan.media_warning;
      node('native-note').textContent = nativeRestart ? 'Application occurs after WAMF stops its workers and restarts.' : 'Restore and restart requires the native WAMF supervisor.';
    }
    if (['pending_restart', 'applying', 'preparing', 'migrating', 'rollback_ready', 'replacing', 'replaced', 'startup_pending'].includes(data.phase)) {
      restarting = true;
      restartStart ||= Date.now();
    } else if (['complete', 'failed', 'rejected', 'recovery_required', 'cancelled'].includes(data.phase)) {
      restarting = false;
      restartStart = 0;
    }
    message(`${labels[data.phase] || data.phase}${data.validation_code ? `: ${data.validation_code}` : ''}${data.application ? ` — ${data.application.message}` : ''}`);
    controls();
  }
  async function catalog() {
    const {backups} = await api('/backups');
    node('backups').replaceChildren();
    if (!backups.length) node('backups').textContent = 'No managed database backups yet.';
    for (const entry of backups) {
      const li = document.createElement('li');
      const text = document.createElement('div');
      text.textContent = `${entry.timestamp || 'Timestamp unavailable'} · ${entry.size == null ? 'Unknown size' : `${(entry.size / 1048576).toFixed(2)} MiB`} · ${entry.schema || entry.compatibility || 'Unknown schema'} · WAMF ${entry.wamf_version || 'unknown'}${entry.valid ? '' : ` · Invalid: ${entry.code}`}`;
      li.append(text);
      if (entry.valid) {
        const download = document.createElement('a');
        download.textContent = 'Download'; download.href = `${base}/backups/${encodeURIComponent(entry.backup_id)}/download`;
        const select = document.createElement('button'); select.type = 'button';
        select.className = 'settings-button settings-button-secondary'; select.textContent = 'Validate for restore';
        select.dataset.backupId = entry.backup_id;
        select.addEventListener('click', () => action(async () => {
          node('confirm-check').checked = false;
          beginJob(await post('/restore/stage', {backup_id: entry.backup_id}));
        }));
        li.append(download, select);
      }
      node('backups').append(li);
    }
    controls();
  }
  const post = (path, data) => api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  function beginJob(data) {
    job = data.job_id; sessionStorage.setItem('wamf-database-job', job);
    busy = true; node('cancel-job').hidden = false; message('Database operation started…');
  }
  async function action(work) {
    if (busy || restarting) return;
    busy = true; controls();
    try { await work(); } catch (error) { message(error.message); }
    finally { if (!job) busy = false; controls(); }
  }
  async function poll() {
    try {
      const status = await api('/status');
      if (restartInstance && status.instance_id !== restartInstance) {
        restartInstance = null;
        sessionStorage.removeItem('wamf-database-restart-instance');
      }
      nativeRestart = status.native_restart; holdOperation = status.hold_operation_id;
      node('hold').hidden = !status.retention_hold;
      if (!job && status.job && !status.job.done) { job = status.job.job_id; sessionStorage.setItem('wamf-database-job', job); }
      if (job) {
        let snapshot;
        try { snapshot = await api(`/jobs/${encodeURIComponent(job)}`); }
        catch (error) { if (error.status !== 404) throw error; snapshot = {done: true, phase: 'failed', result: {message: 'Operation interrupted by restart; check backups and durable restore status.'}}; }
        busy = !snapshot.done; node('cancel-job').hidden = snapshot.done;
        if (snapshot.operation_id) saveOperation(snapshot.operation_id);
        message(`${labels[snapshot.phase] || snapshot.phase}${snapshot.percent != null ? ` (${snapshot.percent}% of copy)` : ''}`);
        if (snapshot.done) {
          job = null; sessionStorage.removeItem('wamf-database-job');
          if (snapshot.result?.message) message(snapshot.result.message);
          await catalog();
        }
      }
      if (!busy && status.operation && (!operationId || status.operation.operation_id === operationId)) { preview(status.operation); }
      else if (operationId && !busy) {
        try { preview(await api(`/operations/${encodeURIComponent(operationId)}`)); }
        catch (error) { if (error.status === 409 || error.status === 404) { saveOperation(null); node('preview').hidden = true; } else throw error; }
      }
      controls();
      if (!job && !operationId && !status.operation && node('status').textContent === 'Loading database status…') message('Ready to create or validate a database backup.');
    } catch (error) {
      if (restarting && Date.now() - restartStart < 330000) message('WAMF is restarting. Waiting for the new instance; Restore will not be submitted again.');
      else { message(error.message); if (restarting) node('recovery').hidden = false; }
    } finally { timer = window.setTimeout(poll, job || restarting ? 750 : 4000); }
  }
  node('create').addEventListener('click', () => action(async () => beginJob(await post('/backups', {}))));
  node('refresh').addEventListener('click', () => catalog().catch(error => message(error.message)));
  node('upload-form').addEventListener('submit', event => {
    event.preventDefault();
    action(async () => { node('confirm-check').checked = false; message('Uploading and validating package…'); beginJob(await api('/restore/upload', {method: 'POST', headers: {'Content-Type': 'application/zip'}, body: node('package').files[0]})); });
  });
  node('confirm-check').addEventListener('change', controls);
  node('ack-check').addEventListener('change', controls);
  node('cancel-job').addEventListener('click', () => { if (job) post(`/jobs/${encodeURIComponent(job)}/cancel`, {}).catch(error => message(error.message)); });
  node('cancel-restore').addEventListener('click', () => action(async () => preview(await post(`/operations/${encodeURIComponent(operationId)}/cancel`, {}))));
  node('apply').addEventListener('click', () => action(async () => {
    if (!operation?.plan || !node('confirm-check').checked) return;
    const queued = await post(`/operations/${encodeURIComponent(operationId)}/restart`, {confirmation_token: operation.plan.confirmation_token, destructive_confirmation: true});
    restartInstance = queued.instance_id;
    sessionStorage.setItem('wamf-database-restart-instance', restartInstance);
    restarting = true; restartStart = Date.now(); node('preview').hidden = true;
    message('Restore queued for controlled restart. Do not repeat the request.');
  }));
  node('ack').addEventListener('click', () => action(async () => {
    if (!node('ack-check').checked) return;
    await post('/retention-hold/acknowledge', {operation_id: holdOperation, acknowledge_media_mismatch: true});
    node('hold').hidden = true; node('ack-check').checked = false; message('Restore-related retention hold acknowledged. Saved retention policy may run again.');
  }));
  window.addEventListener('pagehide', () => window.clearTimeout(timer));
  catalog().catch(error => message(error.message));
  poll();
})();
