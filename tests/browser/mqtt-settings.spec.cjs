const { test, expect } = require('@playwright/test');
const { openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

const endpoint = '**/admin/config/mqtt/test';
const successPresentation = 'Connection successful Connected to the MQTT broker using the saved settings.';
const successMessage = 'Broker accepted an MQTT connection using the saved settings.';
function outcome(code = 'success', safe_message = successMessage) {
  return { success: code === 'success', code, safe_message, duration_ms: 12, completed_at: '2026-10-18T12:00:00+00:00' };
}
async function respond(route, result = outcome()) {
  await route.fulfill({ json: result, headers: { 'Cache-Control': 'no-store' } });
}
async function testConnection(page) {
  await page.locator('#mqtt').getByRole('button', { name: 'Test connection', exact: true }).click();
  await expect(page.locator('#mqtt-test-result')).toContainText('Connection successful');
}

test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // Session failures and aborted fetches intentionally produce network console errors.
  page.__mqttErrors = errors;
  await openAdmin(page, '/admin/config#mqtt');
});
test.afterEach(async ({ page }) => {
  expect(page.__mqttErrors).toEqual([]);
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
  const button = page.locator('#mqtt-test-button');
  await expect(button).toBeVisible();
  await expect(page.getByText('Uses the saved MQTT settings. Save changes before testing.')).toBeVisible();
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
  await page.locator('#mqtt_host').focus();
  release();
  const result = page.locator('#mqtt-test-result');
  await expect(result).toContainText(successPresentation);
  await expect(result).toHaveAttribute('role', 'status');
  await expect(result).toHaveAttribute('aria-live', 'polite');
  await expect(button).toBeEnabled();
  await expect(page.locator('#mqtt_host')).toBeFocused();
  await expect(page.getByText('This checks the broker connection only, not event delivery.')).toBeVisible();
  expect(await page.content()).not.toContain('M2-SECRET-');
  await expect(page.locator('#mqtt_username')).toHaveValue('');
  await expect(page.locator('#mqtt_password')).toHaveValue('');
  await noHorizontalOverflow(page);
});

for (const [code, message] of [
  ['auth_rejected', 'Broker rejected authentication or authorization.'],
  ['timeout', 'Connection test timed out.'],
]) {
  test(`inline sanitized ${code} result`, async ({ page }) => {
    await page.route(endpoint, route => respond(route, outcome(code, message)));
    await page.locator('#mqtt').getByRole('button', { name: 'Test connection', exact: true }).click();
    await expect(page.locator('#mqtt-test-result')).toHaveText(`Failed: ${message}`);
    await expect(page.locator('#mqtt-test-button')).toBeEnabled();
    expect(await page.content()).not.toContain('M2-SECRET-');
    await noHorizontalOverflow(page);
  });
}

test('connection edits invalidate results; topic edits do not', async ({ page }) => {
  await page.route(endpoint, route => respond(route));
  if (!await page.locator('#mqtt_authentication_enabled').isChecked()) {
    await page.locator('label[for="mqtt_authentication_enabled"]').click();
  }
  if (!await page.locator('#mqtt_tls_enabled').isChecked()) {
    await page.locator('label[for="mqtt_tls_enabled"]').click();
  }
  const changes = [
    ['mqtt_host', 'edited-broker'], ['mqtt_port', '2883'],
    ['mqtt_username', 'unsaved-user'], ['mqtt_password', 'unsaved-password'],
    ['mqtt_tls_ca_certs', '/new/ca.pem'],
  ];
  for (const [id, value] of changes) {
    await testConnection(page);
    await page.locator(`#${id}`).fill(value);
    await expect(page.locator('#mqtt-test-result')).toContainText('stale');
  }
  for (const id of ['mqtt_tls_insecure', 'mqtt_authentication_enabled', 'mqtt_tls_enabled']) {
    await testConnection(page);
    await page.locator(`label[for="${id}"]`).click();
    await expect(page.locator('#mqtt-test-result')).toContainText('stale');
  }
  await testConnection(page);
  await page.locator('#mqtt_topic_prefix').fill('changed-topic');
  await expect(page.locator('#mqtt-test-result')).toHaveText(successPresentation);
  await noHorizontalOverflow(page);
});

test('edits during pending request mark eventual result stale', async ({ page }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(endpoint, async route => { await gate; await respond(route); });
  await page.locator('#mqtt-test-button').click();
  await expect(page.locator('#mqtt-test-button')).toBeDisabled();
  await page.locator('#mqtt_host').fill('unsaved-broker');
  release();
  await expect(page.locator('#mqtt-test-result')).toContainText('stale');
});

test('lost response is restored without automatic resubmission', async ({ page }) => {
  let count = 0;
  await page.route(endpoint, route => { count += 1; return route.abort(); });
  await page.locator('#mqtt-test-button').click();
  await expect(page.locator('#mqtt-test-result')).toContainText('outcome is unknown');
  await expect(page.locator('#mqtt-test-button')).toBeEnabled();
  expect(count).toBe(1);
});

for (const status of [400, 401, 503]) {
  test(`safe handling of HTTP ${status}`, async ({ page }) => {
    await page.route(endpoint, route => route.fulfill({ status, json: { error: 'M2-SECRET-RAW-EXCEPTION' } }));
    await page.locator('#mqtt-test-button').click();
    await expect(page.locator('#mqtt-test-result')).toContainText(
      status === 400 ? 'Security token expired' : status === 401 ? 'Admin login required' : 'could not be completed',
    );
    await expect(page.locator('#mqtt-test-button')).toBeEnabled();
    expect(await page.content()).not.toContain('M2-SECRET-');
  });
}

test('expired session follows login redirect convention', async ({ page }) => {
  await page.route('**/login?next=mqtt-test', route => route.fulfill({ contentType: 'text/html', body: '<h1>Log in</h1>' }));
  await page.route(endpoint, route => route.fulfill({ status: 302, headers: { location: '/login?next=mqtt-test' } }));
  await page.locator('#mqtt-test-button').click();
  await expect(page).toHaveURL(/\/login\?next=mqtt-test$/);
});
