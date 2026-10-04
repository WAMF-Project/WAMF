const { test, expect, openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

test.beforeEach(async ({ page }) => {
  await openAdmin(page);
  await expect(page.getByRole('heading', { name: 'System Health', level: 1 })).toBeVisible();
});

test('System Health services, compact navigation and responsive layout', async ({ page }) => {
  const health = page.getByRole('region', { name: 'System Health', exact: true });
  for (const label of ['Frigate', 'MQTT', 'Database', 'Archive Storage', 'System Disk']) {
    const row = health.locator('dl > div').filter({ has: page.getByText(label, { exact: true }) });
    await expect(row.locator('dt')).toBeVisible();
    await expect(row.locator('dd')).toBeVisible();
    await expect(row.locator('dd')).toContainText(/Online|Offline|Healthy|Error|Writable|Problem|Setup required|Normal|Elevated|High/);
    // Readable rows fit within the service card, with no clipped text.
    expect(await row.evaluate(element => {
      const parent = element.closest('.admin-health-card').getBoundingClientRect();
      const rect = element.getBoundingClientRect();
      return rect.left >= parent.left && rect.right <= parent.right + 1 &&
        [...element.querySelectorAll('dt, dd')].every(child => child.scrollWidth <= child.clientWidth + 1);
    })).toBeTruthy();
  }

  const menu = page.getByRole('button', { name: 'Menu', exact: true });
  const mobile = await menu.isVisible();
  if (mobile) {
    await menu.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await visibleKeyboardFocus(menu);
    await page.keyboard.press('Enter');
    await expect(menu).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('#app-sidebar').getByRole('button', { name: 'Close navigation', exact: true })).toBeFocused();
  }
  const sidebar = page.getByRole('complementary', { name: 'Application navigation' });
  await expect(sidebar).toBeVisible();
  const summary = sidebar.getByRole('region', { name: 'System Health' });
  await expect(summary.getByRole('link')).toContainText(/All systems OK|Setup required|Attention needed|Service issue/);
  for (const label of ['Frigate', 'MQTT', 'Database', 'Archive Storage', 'System Disk']) {
    await expect(sidebar.getByText(label, { exact: true })).toHaveCount(0);
  }
  await expect(sidebar.getByRole('link', { name: 'Settings', exact: true })).toBeVisible();
  if (mobile) {
    await page.keyboard.press('Escape');
    await expect(menu).toHaveAttribute('aria-expanded', 'false');
    await visibleKeyboardFocus(menu);
    await expect(page.locator('.app-main')).not.toHaveAttribute('inert', '');
  }
  await noHorizontalOverflow(page);
  const retentionSummary = page.locator('.admin-retention-summary');
  expect(await retentionSummary.evaluate(element => element.scrollWidth <= element.clientWidth + 1),
    'Retention summary wraps within its card').toBeTruthy();
});

test('Retention presentation and keyboard disclosure', async ({ page }) => {
  const retention = page.getByRole('region', { name: 'Retention', exact: true });
  await expect(retention.getByText('Latest attempt status', { exact: true })).toBeVisible();
  await expect(retention.locator('.admin-retention-outcome')).toHaveText(/\S/);
  await expect(retention.getByText('Latest attempt completed', { exact: true })).toBeVisible();
  await expect(retention.locator('.admin-retention-completed')).toHaveText(/\S/);
  for (const label of ['Configured automatic cleanup', 'Trigger']) {
    const row = retention.locator('dl > div').filter({ has: page.getByText(label, { exact: true }) });
    await expect(row.locator('dt')).toBeVisible();
    await expect(row.locator('dd')).toHaveText(/\S/);
  }
  for (const label of ['Snapshots deleted', 'Clips deleted', 'Orphans found', 'Orphan files deleted',
    'Missing references found', 'System events pruned', 'Error count']) {
    await expect(retention.getByText(label, { exact: true })).toBeVisible();
  }
  const details = retention.locator('details');
  const disclosure = details.locator('summary');
  await expect(details).not.toHaveAttribute('open', '');
  await disclosure.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await visibleKeyboardFocus(disclosure);
  await page.keyboard.press('Enter');
  await expect(details).toHaveAttribute('open', '');
  for (const label of ['Expired-media action', 'Orphan-media action', 'Latest attempt started', 'Rows scanned']) {
    await expect(details.getByText(label, { exact: true })).toBeVisible();
  }
  await noHorizontalOverflow(page);
  await page.keyboard.press('Space');
  await expect(details).not.toHaveAttribute('open', '');
});

test('System Health capture @screenshot', async ({ page }, testInfo) => {
  await noHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`system-health-${testInfo.project.name}.png`), fullPage: true });
});
