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
