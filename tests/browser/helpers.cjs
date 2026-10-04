const { test: base, expect } = require('@playwright/test');

async function openAdmin(page, path = '/admin') {
  let response = await page.goto(path);
  if (new URL(page.url()).pathname === '/login') {
    const password = process.env.WAMF_ADMIN_PASSWORD;
    if (!password) {
      throw new Error('Admin login required: set WAMF_ADMIN_PASSWORD or WAMF_BROWSER_STORAGE_STATE (npm run browser:login).');
    }
    await page.getByLabel('Admin password').fill(password);
    await Promise.all([
      page.waitForURL(url => url.pathname !== '/login'),
      page.getByRole('button', { name: 'Log in', exact: true }).click(),
    ]);
    response = await page.goto(path);
  }
  expect(response.ok(), 'Admin page HTTP response').toBeTruthy();
}

const test = base.extend({
  page: async ({ page }, use) => {
    const errors = [];
    // No broad filtering: only favicon 404 noise is accepted.
    page.on('console', message => {
      if (message.type() !== 'error') return;
      const { url } = message.location();
      if (url && new URL(url).pathname === '/favicon.ico' && /404/.test(message.text())) return;
      errors.push(`console: ${message.text()}`);
    });
    page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
    await use(page);
    expect(errors, 'Unexpected browser console / JavaScript errors').toEqual([]);
  },
});

async function noHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => Math.max(
    document.documentElement.scrollWidth, document.body.scrollWidth,
  ) - window.innerWidth);
  expect(overflow, 'Horizontal page overflow in CSS pixels').toBeLessThanOrEqual(1);
}

async function visibleKeyboardFocus(locator) {
  await expect(locator).toBeFocused();
  expect(await locator.evaluate(element => {
    const style = getComputedStyle(element);
    return element.matches(':focus-visible') && (
      (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0) ||
      style.boxShadow !== 'none'
    );
  }), 'Keyboard focus has a visible outline or shadow').toBeTruthy();
}

module.exports = { test, expect, openAdmin, noHorizontalOverflow, visibleKeyboardFocus };
