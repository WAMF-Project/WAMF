const { test, expect, openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

test.skip(process.env.WAMF_BROWSER_ISOLATED !== '1', 'Runtime scenarios require the isolated fake-backed app.');

for (const [mode, label] of [
  ['connected', 'Connected'], ['subscribing', 'Subscribing'], ['starting', 'Starting'],
  ['reconnecting', 'Reconnecting'], ['disconnected', 'Disconnected'],
  ['failed', 'Subscription failed'], ['unknown', 'Unknown'], ['stale', 'Stale'],
  ['setup', 'Setup required'], ['quiet', 'Connected'],
]) {
  test(`production MQTT ${mode} presentation`, async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-WAMF-Test-MQTT-State': mode });
    await openAdmin(page);
    const tile = page.locator('#mqtt-runtime-tile');
    await expect(tile).toContainText(label);
    expect(await tile.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
    const details = page.locator('.admin-mqtt-details');
    const summary = details.locator('summary');
    await expect(details).not.toHaveAttribute('open', '');
    await summary.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await visibleKeyboardFocus(summary);
    await page.keyboard.press('Enter');
    await expect(details).toHaveAttribute('open', '');
    const field = name => details.locator('dl > div').filter({ has: page.getByText(name, { exact: true }) }).locator('dd');
    await expect(field('Runtime status')).toHaveText(label);
    if (mode !== 'setup') await expect(field('Configured broker')).toHaveText('saved-broker:1883');
    await expect(field('Configured event topic')).toHaveText('frigate/events');
    if (mode === 'quiet' || mode === 'unknown') {
      await expect(field('Last event message received')).toHaveText('No events received yet');
    } else {
      await expect(field('Last event message received')).toContainText('18 Oct 2026');
    }
    if (mode === 'stale') await expect(field('Runtime freshness')).toContainText('historical');
    if (mode === 'reconnecting') await expect(field('Disconnect reason')).toHaveText('Unexpected disconnection');
    if (mode !== 'connected' && mode !== 'quiet') {
      await expect(page.locator('.app-sidebar')).not.toContainText('All systems OK');
    }
    expect(await page.content()).not.toContain('M2-SECRET-');
    await expect(details).toContainText('Times shown in');
    await noHorizontalOverflow(page);
    await summary.focus();
    await page.keyboard.press('Space');
    await expect(details).not.toHaveAttribute('open', '');
  });
}
