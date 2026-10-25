const { execFileSync } = require('node:child_process');
const { test, expect, openAdmin, noHorizontalOverflow } = require('./helpers.cjs');

// Mutating coverage must never run against a live WAMF instance.
test.skip(process.env.WAMF_BROWSER_ISOLATED !== '1', 'Database workflow tests require the disposable fixture.');
const base = '/admin/database';

test.beforeEach(async ({ page }) => {
  await page.context().setExtraHTTPHeaders({ 'X-WAMF-Test-Database-Auth': 'enabled' });
});

async function csrf(page) {
  return page.locator('#database-tools').getAttribute('data-csrf');
}
async function createPackage(page) {
  const response = await page.request.post(base + '/backups', {data: {}, headers: {'X-CSRF-Token': await csrf(page)}});
  expect(response.status()).toBe(202);
  const { job_id } = await response.json();
  let job;
  await expect.poll(async () => {
    job = await (await page.request.get(`${base}/jobs/${job_id}`)).json();
    return job.done;
  }, {timeout: 20000}).toBeTruthy();
  expect(job.result.success).toBeTruthy();
  return job.result.backup_id;
}

test('disabled auth exposes no database actions', async ({ page }) => {
  await page.context().setExtraHTTPHeaders({});
  await openAdmin(page);
  const panel = page.getByRole('region', {name: 'Database Backup & Restore', exact: true});
  await expect(panel).toContainText('Enable Admin authentication');
  await expect(panel.getByRole('button')).toHaveCount(0);
  expect((await page.request.get(base + '/backups')).status()).toBe(403);
  await noHorizontalOverflow(page);
});

test('backup catalog, validated download and corrupt artifact presentation', async ({ page }) => {
  await openAdmin(page);
  const identifier = await createPackage(page);
  await page.route('**/admin/database/backups', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    data.backups.push({backup_id: 'corrupt', valid: false, code: 'invalid_package', size: 50});
    await route.fulfill({response, json: data});
  });
  await page.getByRole('button', {name: 'Refresh backups'}).click();
  const panel = page.locator('#database-tools');
  await expect(panel).toContainText('Invalid: invalid_package');
  await expect(panel).toContainText('006_versioned_schema_baseline');
  const event = page.waitForEvent('download');
  await panel.locator(`a[href$="/${identifier}/download"]`).click();
  expect((await event).suggestedFilename()).toBe(identifier + '.zip');
  expect((await page.request.post(base + '/backups', {data: {}})).status()).toBe(400);
  await noHorizontalOverflow(page);
});

test('backup progress is visible and prevents duplicate clicks', async ({ page }) => {
  let finished = false, requests = 0;
  await page.route('**/admin/database/backups', async route => {
    if (route.request().method() === 'GET') return route.continue();
    requests += 1;
    await route.fulfill({status: 202, json: {job_id: 'browser-progress'}});
  });
  await page.route('**/admin/database/jobs/browser-progress', route => route.fulfill({json: {
    job_id: 'browser-progress', kind: 'backup', phase: finished ? 'complete' : 'backing_up',
    percent: finished ? 100 : 37, done: finished, result: finished ? {success: true, message: 'Backup created and verified.'} : null,
  }}));
  await openAdmin(page);
  await page.getByRole('button', {name: 'Create backup', exact: true}).click();
  await expect(page.locator('#database-status')).toContainText('37% of copy');
  await expect(page.locator('#database-create')).toBeDisabled();
  expect(requests).toBe(1);
  finished = true;
  await expect(page.locator('#database-create')).toBeEnabled();
});

test('managed preview, exact confirmation and refresh never repost restart', async ({ page }) => {
  let requested = false, restartCount = 0, operation;
  await page.route('**/admin/database/status', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.native_restart = true;
    if (requested) {
      data.instance_id = 'new-fixture-instance';
      data.operation = {operation_id: operation, phase: 'complete', code: 'complete', application: {message: 'Restored database startup succeeded.'}};
    }
    await route.fulfill({response, json: data});
  });
  await page.route('**/admin/database/operations/*/restart', async route => {
    restartCount += 1;
    const body = route.request().postDataJSON();
    expect(body.destructive_confirmation).toBe(true);
    expect(body.confirmation_token).toMatch(/^[0-9a-f]{64}$/);
    operation = new URL(route.request().url()).pathname.split('/').at(-2);
    requested = true;
    await route.fulfill({json: {success: true, phase: 'pending_restart', instance_id: 'old-fixture-instance'}});
  });
  await openAdmin(page);
  const identifier = await createPackage(page);
  await page.locator('#database-refresh').click();
  await page.locator(`[data-backup-id="${identifier}"]`).click();
  await expect(page.locator('#database-preview')).toBeVisible();
  await expect(page.locator('#database-preview-schema')).toContainText('Schema is current');
  await expect(page.locator('#database-preview-warning')).toContainText('snapshots and clips are not restored');
  await expect(page.locator('#database-apply')).toBeDisabled();
  await page.reload();
  await expect(page.locator('#database-preview')).toBeVisible();
  await page.locator('#database-confirm-check').check();
  await expect(page.locator('#database-apply')).toBeEnabled();
  await page.locator('#database-apply').click();
  await expect(page.locator('#database-status')).toContainText('Restored database startup succeeded', {timeout: 15000});
  await page.reload();
  await expect(page.locator('#database-status')).toContainText('Restored database startup succeeded');
  expect(restartCount).toBe(1);
  await expect(page.getByRole('button', {name: /rollback/i})).toHaveCount(0);
  await noHorizontalOverflow(page);
});

test('upload validates real package and rejects a newer schema', async ({ page }) => {
  await openAdmin(page);
  const identifier = await createPackage(page);
  const data = await (await page.request.get(`${base}/backups/${identifier}/download`)).body();
  await page.locator('#database-restore summary').click();
  await page.locator('#database-package').setInputFiles({name: '../../untrusted.zip', mimeType: 'application/zip', buffer: data});
  await page.locator('#database-upload').click();
  await expect(page.locator('#database-preview')).toBeVisible();
  await expect(page.locator('#database-preview-schema')).toContainText('current');
  await page.locator('#database-cancel-restore').click();
  await expect(page.locator('#database-preview')).toBeHidden();
  const future = execFileSync('.venv312/bin/python', ['-B', '-c', `
import io, json, hashlib, sqlite3, zipfile
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
import wamf_paths
from app import db
from tests.test_database_backup import manifest_for
with TemporaryDirectory() as directory:
    wamf_paths.REPO_ROOT = Path(directory)
    path = Path(directory) / 'future.db'
    db.ensure_schema(path)
    manifest = manifest_for(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_migrations(version) VALUES ('007_future')")
    payload = path.read_bytes()
    manifest = replace(manifest, database_size=len(payload), database_sha256=hashlib.sha256(payload).hexdigest())
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('database.db', payload)
        archive.writestr('manifest.json', manifest.to_bytes())
    import sys
    sys.stdout.buffer.write(output.getvalue())
`]);
  await page.locator('#database-package').setInputFiles({name: 'future.zip', mimeType: 'application/zip', buffer: future});
  await page.locator('#database-upload').click();
  await expect(page.locator('#database-status')).toContainText('newer_database', {timeout: 20000});
  await expect(page.locator('#database-preview')).toBeHidden();
});

test('retention hold warning requires explicit acknowledgement', async ({ page }) => {
  let held = true, ackCount = 0;
  const identifier = 'restore-' + 'a'.repeat(32);
  await page.route('**/admin/database/status', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.retention_hold = held; data.hold_operation_id = identifier;
    await route.fulfill({response, json: data});
  });
  await page.route('**/admin/database/retention-hold/acknowledge', async route => {
    expect(route.request().postDataJSON()).toEqual({operation_id: identifier, acknowledge_media_mismatch: true});
    ackCount += 1; held = false;
    await route.fulfill({json: {success: true, retention_hold: false}});
  });
  await openAdmin(page);
  await expect(page.locator('#database-hold')).toBeVisible();
  await expect(page.locator('#database-hold')).toContainText('newer media may appear orphaned');
  await expect(page.locator('#database-ack')).toBeDisabled();
  await page.locator('#database-ack-check').check();
  await page.locator('#database-ack').click();
  await expect(page.locator('#database-hold')).toBeHidden();
  expect(ackCount).toBe(1);
});
