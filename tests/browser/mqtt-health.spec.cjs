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
    await expect(field('Configured review topic')).toHaveText('frigate/reviews');
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

for (const [mode, capability] of [
  ['reviews-api-only', 'Available'], ['reviews-degraded', 'Degraded'],
  ['reviews-unavailable', 'Unavailable'],
]) {
  test(`review ingestion ${mode} diagnostics`, async ({ page }) => {
    await page.setExtraHTTPHeaders({ 'X-WAMF-Test-MQTT-State': mode });
    await openAdmin(page);
    await expect(page.locator('#mqtt-runtime-tile')).toContainText('Connected');
    const details = page.locator('.admin-mqtt-details');
    await details.locator('summary').click();
    const field = name => details.locator('dl > div').filter({ has: page.getByText(name, { exact: true }) }).locator('dd');
    await expect(field('Review capability')).toHaveText(capability);
    await expect(field('Review subscription')).toHaveText('subscribed');
    if (mode === 'reviews-api-only') {
      await expect(field('Last review message received')).toHaveText('No reviews received yet');
      await expect(field('Active reviews')).toHaveText('2');
      await expect(field('Unmatched tracked events')).toHaveText('3');
      await expect(field('Terminal classification pending / retrying')).toHaveText('3 / 1');
      await expect(field('Terminal classified / rejected / failed')).toHaveText('4 / 2 / 1');
      await expect(field('Reviews ready / finalized / aggregation failures')).toHaveText('1 / 5 / 0');
      await expect(field('Grouped species observations')).toHaveText('7');
    }
    expect(await page.content()).not.toContain('M2-SECRET-');
    await noHorizontalOverflow(page);
  });
}
