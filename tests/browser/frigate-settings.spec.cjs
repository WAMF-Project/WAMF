const { test, expect } = require('@playwright/test');
const { openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

const endpoint = '**/admin/config/frigate/test';
const successPresentation = 'Connection successful Frigate API responded using the saved settings.';
const successMessage = 'Frigate API responded using the saved settings.';
function outcome(code = 'success', safe_message = successMessage) {
  return { success: code === 'success', code, safe_message, duration_ms: 12, completed_at: '2026-10-18T12:00:00+00:00' };
}
async function respond(route, result = outcome()) {
  await route.fulfill({ json: result, headers: { 'Cache-Control': 'no-store' } });
}
async function testConnection(page) {
  await page.locator('#frigate').getByRole('button', { name: 'Test connection', exact: true }).click();
  await expect(page.locator('#frigate-test-result')).toContainText('Connection successful');
}

test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // Session failures and aborted fetches intentionally produce network console errors.
  page.__frigateErrors = errors;
  await openAdmin(page, '/admin/config#frigate');
});
test.afterEach(async ({ page }) => {
  expect(page.__frigateErrors).toEqual([]);
});

test('saved-only helper, keyboard activation, pending and duplicate suppression', async ({ page }) => {
  let count = 0;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(endpoint, async route => {
    count += 1;
    expect(route.request().postData()).toBeNull();
    await gate;
    await respond(route);
  });
  const button = page.locator('#frigate-test-button');
  await expect(button).toBeVisible();
  await expect(page.getByText('Uses the saved Frigate settings. Save changes before testing.')).toBeVisible();
  await button.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await visibleKeyboardFocus(button);
  await page.keyboard.press('Enter');
  await expect(button).toBeDisabled();
  await expect(button).toHaveText('Testing…');
  await expect.poll(() => count).toBe(1);
  await button.evaluate(element => { element.click(); element.dispatchEvent(new MouseEvent('click')); });
  expect(count).toBe(1);
  // Completion must not take focus away from another control.
  await page.locator('#frigate_url').focus();
  release();
  const result = page.locator('#frigate-test-result');
  await expect(result).toContainText(successPresentation);
  await expect(result).toHaveAttribute('role', 'status');
  await expect(result).toHaveAttribute('aria-live', 'polite');
  await expect(button).toBeEnabled();
  await expect(page.locator('#frigate_url')).toBeFocused();
  await expect(page.getByText('This checks the Frigate API connection only, not camera availability.')).toBeVisible();
  expect(await page.content()).not.toContain('F1-SECRET-');
  await noHorizontalOverflow(page);
});

for (const [code, message] of [
  ['auth_rejected', 'Frigate rejected authentication or authorization.'],
  ['timeout', 'Connection test timed out.'],
]) {
  test(`inline sanitized ${code} result`, async ({ page }) => {
    await page.route(endpoint, route => respond(route, outcome(code, message)));
    await page.locator('#frigate').getByRole('button', { name: 'Test connection', exact: true }).click();
    await expect(page.locator('#frigate-test-result')).toHaveText(`Failed: ${message}`);
    await expect(page.locator('#frigate-test-button')).toBeEnabled();
    expect(await page.content()).not.toContain('F1-SECRET-');
    await noHorizontalOverflow(page);
  });
}

test('only Frigate URL edits invalidate the result', async ({ page }) => {
  await page.route(endpoint, route => respond(route));
  await testConnection(page);
  await page.locator('#frigate_url').fill('http://edited-frigate');
  await expect(page.locator('#frigate-test-result')).toContainText('stale');
  await testConnection(page);
  for (const [id, value] of [
    ['frigate_cameras', 'manual-camera'], ['frigate_object', 'other'],
    ['live_view_url', 'http://live-view'], ['mqtt_host', 'other-broker'],
  ]) {
    await page.locator(`#${id}`).fill(value);
    await expect(page.locator('#frigate-test-result')).toHaveText(successPresentation);
  }
});

test('edits during pending request mark eventual result stale', async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(endpoint, async route => { await gate; await respond(route); });
  await page.locator('#frigate-test-button').click();
  await expect(page.locator('#frigate-test-button')).toBeDisabled();
  await page.locator('#frigate_url').fill('unsaved-broker');
  release();
  await expect(page.locator('#frigate-test-result')).toContainText('stale');
  await expect(page.locator('#frigate-test-result')).not.toContainText('Connection successful');
});

test('lost response is restored without automatic resubmission', async ({ page }) => {
  let count = 0;
  await page.route(endpoint, route => { count += 1; return route.abort(); });
  await page.locator('#frigate-test-button').click();
  await expect(page.locator('#frigate-test-result')).toContainText('outcome is unknown');
  await expect(page.locator('#frigate-test-button')).toBeEnabled();
  expect(count).toBe(1);
});

for (const status of [400, 401, 503]) {
  test(`safe handling of HTTP ${status}`, async ({ page }) => {
    await page.route(endpoint, route => route.fulfill({ status, json: { error: 'F1-SECRET-RAW-EXCEPTION' } }));
    await page.locator('#frigate-test-button').click();
    await expect(page.locator('#frigate-test-result')).toContainText(
      status === 400 ? 'Security token expired' : status === 401 ? 'Admin login required' : 'could not be completed',
    );
    await expect(page.locator('#frigate-test-button')).toBeEnabled();
    expect(await page.content()).not.toContain('F1-SECRET-');
  });
}

test('expired session follows login redirect convention', async ({ page }) => {
  await page.route('**/login?next=frigate-test', route => route.fulfill({ contentType: 'text/html', body: '<h1>Log in</h1>' }));
  await page.route(endpoint, route => route.fulfill({ status: 302, headers: { location: '/login?next=frigate-test' } }));
  await page.locator('#frigate-test-button').click();
  await expect(page).toHaveURL(/\/login\?next=frigate-test$/);
});

const discoveryEndpoint = '**/admin/config/frigate/cameras/discover';
const inventory = [
  { id: 'garden', display_name: 'Garden birds', enabled: true, detection_enabled: true },
  { id: 'Garden_ID', display_name: 'Side feeder', enabled: false, detection_enabled: null },
  { id: 'Quiet_ID', display_name: 'Quiet_ID', enabled: true, detection_enabled: false },
];
function cameraCheckbox(page, id) {
  return page.locator(`#frigate-camera-list input[value="${id}"]`);
}
async function discoveryReply(route, cameras = inventory, code = 'success') {
  await route.fulfill({ json: {
    success: code === 'success', code,
    safe_message: code === 'success' ? 'Cameras discovered.' : 'Frigate rejected authentication or authorization.',
    cameras: code === 'success' ? cameras : null,
  }, headers: { 'Cache-Control': 'no-store' } });
}
async function discover(page) {
  await page.locator('#frigate-discover-button').click();
  await expect(page.locator('#frigate-discovery-result')).toContainText('Cameras discovered');
}

test('discovery uses saved settings, supports keyboard, suppresses duplicates and preserves focus', async ({ page }) => {
  let count = 0;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(discoveryEndpoint, async route => {
    count += 1;
    expect(route.request().method()).toBe('POST');
    expect(route.request().postData()).toBeNull();
    expect(route.request().headers()['x-csrftoken']).toBeTruthy();
    await gate;
    await discoveryReply(route);
  });
  const button = page.locator('#frigate-discover-button');
  await expect(button).toBeVisible();
  await button.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await visibleKeyboardFocus(button);
  await page.keyboard.press('Enter');
  await expect(button).toBeDisabled();
  await expect(button).toHaveText('Discovering…');
  await expect.poll(() => count).toBe(1);
  await button.evaluate(element => { element.click(); element.dispatchEvent(new MouseEvent('click')); });
  expect(count).toBe(1);
  await page.locator('#frigate_cameras').focus();
  release();
  await expect(page.locator('#frigate-discovery-result')).toContainText('Cameras discovered');
  await expect(button).toBeEnabled();
  await expect(page.locator('#frigate_cameras')).toBeFocused();
  await expect(page.locator('#frigate-discovery-result')).toHaveAttribute('aria-live', 'polite');
});

test('discovery keeps current selections and shows friendly names, flags and missing IDs', async ({ page }) => {
  await page.locator('#frigate_cameras').fill('garden\nmanual_ID');
  await page.route(discoveryEndpoint, route => discoveryReply(route));
  await discover(page);
  await expect(cameraCheckbox(page, 'garden')).toBeChecked();
  await expect(cameraCheckbox(page, 'manual_ID')).toBeChecked();
  await expect(cameraCheckbox(page, 'Garden_ID')).not.toBeChecked();
  await expect(page.locator('.settings-camera-option').filter({ hasText: 'Garden_ID' })).toContainText('Disabled');
  await expect(page.locator('.settings-camera-option').filter({ hasText: 'Garden_ID' })).toContainText('Side feeder');
  await expect(page.locator('.settings-camera-option').filter({ hasText: 'Quiet_ID' })).toContainText('Detection disabled');
  await expect(page.locator('.settings-camera-option').filter({ hasText: 'manual_ID' })).toContainText('Not found in latest discovery');
  await expect(page.locator('#frigate_cameras')).toHaveValue('garden\nmanual_ID');
  await cameraCheckbox(page, 'Garden_ID').check();
  await expect(page.locator('#frigate_cameras')).toHaveValue('garden\nmanual_ID\nGarden_ID');
  await expect(cameraCheckbox(page, 'Garden_ID')).toBeFocused();
  await noHorizontalOverflow(page);
});

test('empty successful discovery retains configured and manually entered values', async ({ page }) => {
  await page.locator('#frigate_cameras').fill('garden\nmanual_ID');
  await page.route(discoveryEndpoint, route => discoveryReply(route, []));
  await page.locator('#frigate-discover-button').click();
  await expect(page.locator('#frigate-discovery-result')).toContainText('No cameras returned');
  await expect(page.locator('#frigate_cameras')).toHaveValue('garden\nmanual_ID');
  await expect(cameraCheckbox(page, 'garden')).toBeChecked();
  await expect(cameraCheckbox(page, 'manual_ID')).toBeChecked();
});

test('failed refresh preserves prior inventory and selections; manual editing remains usable', async ({ page }) => {
  await page.route(discoveryEndpoint, route => discoveryReply(route));
  await discover(page);
  await cameraCheckbox(page, 'Garden_ID').check();
  const before = await page.locator('#frigate_cameras').inputValue();
  await page.route(discoveryEndpoint, route => discoveryReply(route, null, 'auth_rejected'));
  await page.locator('#frigate-discover-button').click();
  await expect(page.locator('#frigate-discovery-result')).toContainText('Failed:');
  await expect(page.locator('#frigate_cameras')).toHaveValue(before);
  await expect(cameraCheckbox(page, 'Garden_ID')).toBeChecked();
  await page.locator('#frigate_cameras').fill(before + '\nOfflineManual');
  await expect(cameraCheckbox(page, 'OfflineManual')).toBeChecked();
});

test('discovery and checkbox changes do not save until Save settings is submitted', async ({ page }) => {
  let saves = 0;
  let posted;
  await page.route('**/admin/config/save', async route => {
    saves += 1;
    posted = new URLSearchParams(route.request().postData());
    await route.fulfill({ contentType: 'text/html', body: '<p>Save received</p>' });
  });
  await page.route(discoveryEndpoint, route => discoveryReply(route));
  await discover(page);
  await cameraCheckbox(page, 'Garden_ID').check();
  await cameraCheckbox(page, 'garden').uncheck();
  expect(saves).toBe(0);
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await expect.poll(() => saves).toBe(1);
  expect(posted.get('frigate_cameras')).toBe('Garden_ID');
  expect(posted.toString()).not.toContain('Side feeder');
});

test('intentionally selecting no cameras submits an explicit empty value', async ({ page }) => {
  let posted;
  await page.route('**/admin/config/save', async route => {
    posted = new URLSearchParams(route.request().postData());
    await route.fulfill({ contentType: 'text/html', body: '<p>Save received</p>' });
  });
  await expect(page.locator('#frigate_cameras')).not.toHaveAttribute('required');
  await cameraCheckbox(page, 'garden').uncheck();
  await expect(page.locator('#frigate_cameras')).toHaveValue('');
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await expect.poll(() => posted !== undefined).toBe(true);
  expect(posted.has('frigate_cameras')).toBe(true);
  expect(posted.get('frigate_cameras')).toBe('');
});

test('URL edit discards stale discovery metadata without clearing selections', async ({ page }) => {
  await page.route(discoveryEndpoint, route => discoveryReply(route));
  await discover(page);
  await cameraCheckbox(page, 'Garden_ID').check();
  await page.locator('#frigate_url').fill('http://changed');
  await expect(page.locator('#frigate-discovery-result')).toContainText('stale');
  await expect(cameraCheckbox(page, 'Garden_ID')).toBeChecked();
  await expect(page.locator('#frigate-camera-list')).not.toContainText('Side feeder');
  await expect(cameraCheckbox(page, 'Quiet_ID')).toHaveCount(0);
});

test('URL edit during pending discovery discards the eventual inventory', async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(discoveryEndpoint, async route => { await gate; await discoveryReply(route); });
  await page.locator('#frigate-discover-button').click();
  await expect(page.locator('#frigate-discover-button')).toBeDisabled();
  await page.locator('#frigate_url').fill('http://changed');
  await page.locator('#frigate_cameras').fill('manual_pending');
  release();
  await expect(page.locator('#frigate-discover-button')).toBeEnabled();
  await expect(page.locator('#frigate-discovery-result')).toContainText('stale');
  await expect(cameraCheckbox(page, 'Garden_ID')).toHaveCount(0);
  await expect(cameraCheckbox(page, 'manual_pending')).toBeChecked();
  await expect(page.locator('#frigate_cameras')).toHaveValue('manual_pending');
});

test('unrelated edits preserve discovery and camera edits during a request survive', async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(discoveryEndpoint, async route => { await gate; await discoveryReply(route); });
  await page.locator('#frigate-discover-button').click();
  await page.locator('#frigate_cameras').fill('manual_pending');
  await cameraCheckbox(page, 'manual_pending').focus();
  release();
  await expect(page.locator('#frigate-discovery-result')).toContainText('Cameras discovered');
  await expect(cameraCheckbox(page, 'manual_pending')).toBeChecked();
  await expect(cameraCheckbox(page, 'manual_pending')).toBeFocused();
  for (const [id, value] of [['mqtt_host', 'other-broker'], ['frigate_object', 'other'], ['live_view_url', 'http://live']]) {
    await page.locator(`#${id}`).fill(value);
    await expect(page.locator('#frigate-discovery-result')).toContainText('Cameras discovered');
    await expect(page.locator('#frigate-camera-list')).toContainText('Side feeder');
  }
});

for (const status of [400, 401, 403, 503]) {
  test(`discovery safely handles HTTP ${status}`, async ({ page }) => {
    const before = await page.locator('#frigate_cameras').inputValue();
    await page.route(discoveryEndpoint, route => route.fulfill({ status, json: { error: 'F2-SECRET-RAW' } }));
    await page.locator('#frigate-discover-button').click();
    await expect(page.locator('#frigate-discovery-result')).toContainText(
      status === 400 ? 'Security token expired' : [401, 403].includes(status) ? 'Admin login required' : 'could not be completed',
    );
    await expect(page.locator('#frigate_cameras')).toHaveValue(before);
    expect(await page.content()).not.toContain('F2-SECRET-RAW');
  });
}

test('discovery session expiry follows login redirect', async ({ page }) => {
  await page.route('**/login?next=discovery', route => route.fulfill({ contentType: 'text/html', body: '<h1>Log in</h1>' }));
  await page.route(discoveryEndpoint, route => route.fulfill({ status: 302, headers: { location: '/login?next=discovery' } }));
  await page.locator('#frigate-discover-button').click();
  await expect(page).toHaveURL(/\/login\?next=discovery$/);
});

test('lost discovery response preserves manual values without automatic resubmission', async ({ page }) => {
  let count = 0;
  const before = await page.locator('#frigate_cameras').inputValue();
  await page.route(discoveryEndpoint, route => { count += 1; return route.abort(); });
  await page.locator('#frigate-discover-button').click();
  await expect(page.locator('#frigate-discovery-result')).toContainText('response was not received');
  await expect(page.locator('#frigate-discover-button')).toBeEnabled();
  await expect(page.locator('#frigate_cameras')).toHaveValue(before);
  expect(count).toBe(1);
});

test('long camera identifiers and friendly names wrap without horizontal overflow', async ({ page }, testInfo) => {
  const longId = 'Camera_' + 'LongCaseSensitiveIdentifier'.repeat(10);
  await page.route(discoveryEndpoint, route => discoveryReply(route, [
    { id: longId, display_name: 'Friendly feeder name '.repeat(10), enabled: false, detection_enabled: false },
  ]));
  await discover(page);
  await expect(cameraCheckbox(page, longId)).not.toBeChecked();
  await noHorizontalOverflow(page);
  await page.locator('#frigate').screenshot({ path: `/tmp/wamf-f2-${testInfo.project.name}.png` });
});
