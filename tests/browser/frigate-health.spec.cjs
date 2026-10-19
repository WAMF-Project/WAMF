const { test, expect, openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

test.skip(process.env.WAMF_BROWSER_ISOLATED !== '1', 'Camera scenarios require isolated Frigate observations.');

for (const [mode, label] of [
  ['healthy', 'Healthy'], ['missing', 'Missing'], ['disabled', 'Disabled'],
  ['detection_disabled', 'Detection disabled'], ['unavailable', 'Unavailable'],
  ['unknown', 'Unknown'], ['stale', 'Stale'],
]) {
  test(`monitored camera ${mode}, summary and keyboard disclosure`, async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-WAMF-Test-Frigate-State': mode });
    await openAdmin(page);
    const section = page.getByRole('region', { name: 'Monitored cameras', exact: true });
    await expect(section.locator('.admin-camera-summary')).toHaveText('1 ' + label.toLowerCase());
    const details = section.locator('details');
    const outside = section.locator(':scope > .admin-camera-row');
    const warning = ['missing', 'disabled', 'detection_disabled', 'unavailable'].includes(mode);
    await expect(outside).toHaveCount(warning ? 1 : 0);
    const sidebar = page.locator('#admin-system-status-text');
    await expect(sidebar).toContainText(warning ? 'Attention needed' :
      ['unknown', 'stale'].includes(mode) ? 'Cameras not fully checked' : 'All systems OK');
    if (warning) {
      await expect(details).toHaveCount(0);
      await expect(section.locator('.admin-camera-row')).toHaveCount(1);
      await expect(section.locator('.admin-camera-observed')).toContainText('Inventory checked 02:');
      await expect(section.locator('.admin-camera-observed')).not.toContainText(/T\d\d:|None/);
      await noHorizontalOverflow(page);
      return;
    }
    await expect(details).not.toHaveAttribute('open', '');
    const summary = details.locator('summary');
    await summary.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await visibleKeyboardFocus(summary);
    await page.keyboard.press('Enter');
    await expect(details).toHaveAttribute('open', '');
    await expect(details.locator('.admin-semantic-status')).toContainText(label);
    await expect(details.locator('.admin-camera-observed')).toHaveText(/Inventory checked \d{2}:\d{2} UTC · Runtime checked \d{2}:\d{2} UTC/);
    if (mode === 'unknown' || mode === 'stale') {
      await expect(details.locator('.admin-semantic-status')).toHaveClass(/status-neutral/);
    }
    await noHorizontalOverflow(page);
    await summary.focus();
    await page.keyboard.press('Space');
    await expect(details).not.toHaveAttribute('open', '');
  });
}

test('mixed camera warnings are compact with healthy rows collapsed', async ({ page }) => {
  await page.setExtraHTTPHeaders({ 'X-WAMF-Test-Frigate-State': 'mixed' });
  await openAdmin(page);
  const section = page.locator('.admin-camera-health');
  await expect(section.locator('.admin-camera-summary')).toHaveText('1 healthy · 1 missing · 1 disabled · 1 detection disabled');
  await expect(section.locator(':scope > .admin-camera-row')).toHaveCount(3);
  await expect(section.locator('details .admin-camera-row').first()).not.toBeVisible();
  await expect(section.locator('details .admin-camera-row')).toHaveCount(1);
  await section.locator('summary').click();
  await expect(section.locator('details strong')).toHaveText('garden');
  for (const id of ['missing_camera', 'disabled_camera', 'detection_disabled_camera']) {
    await expect(section.getByText(id, { exact: true })).toHaveCount(1);
  }
  await noHorizontalOverflow(page);
});

for (const mode of ['outage', 'access']) {
  test(`one Frigate ${mode} problem with neutral camera details`, async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-WAMF-Test-Frigate-State': mode });
    await openAdmin(page);
    const section = page.locator('.admin-camera-health');
    await expect(section.getByText(mode === 'outage'
      ? 'Camera health unavailable while Frigate cannot be reached.'
      : 'Camera health access is unavailable.', { exact: true })).toHaveCount(1);
    await expect(section.locator(':scope > .admin-camera-row')).toHaveCount(0);
    await section.locator('summary').click();
    await expect(section.locator('.admin-semantic-status')).toHaveText('–Unknown');
    await expect(section.locator('.admin-camera-observed')).toHaveCount(0);
    await expect(page.locator('#admin-system-status-text')).toContainText('Attention needed');
    await noHorizontalOverflow(page);
  });
}

test('long exact IDs and friendly names wrap inside the camera card', async ({ page }) => {
  await page.setExtraHTTPHeaders({ 'X-WAMF-Test-Frigate-State': 'long' });
  await openAdmin(page);
  const section = page.locator('.admin-camera-health');
  await section.locator('summary').click();
  await expect(section.locator('strong')).toContainText('very_long_camera_identifier');
  expect(await section.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
  await noHorizontalOverflow(page);
});
