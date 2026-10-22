const { test, expect } = require('@playwright/test');
const { openAdmin, noHorizontalOverflow, visibleKeyboardFocus } = require('./helpers.cjs');

const base = '**/admin/config/models';
const install = '**/admin/config/models/bird/aiy-v1/install';
const select = '**/admin/config/models/bird/aiy-v1/select';
const status = '**/admin/config/models/install-status';
const card = page => page.locator('[data-model-version="aiy-v1"]');
function state({installed = false, selected = false, active = false, known = true, stale = false} = {}) {
  const runningKnown = known && !stale;
  return {
    running_label: runningKnown ? `Bird classification · ${active ? 'aiy-v1' : 'bundled-1'}` : 'Unknown — current classifier status is unavailable.',
    selected_label: `Bird classification · ${selected ? 'aiy-v1' : 'bundled-1'}`,
    restart_required: runningKnown && selected !== active, running_known: runningKnown, selected_error: null, custom: !selected,
    models: [
      {model_id: 'bird', version: 'bundled-1', bundled: true, installed: true, valid: true,
        selected: !selected, active: runningKnown && !active,
        selected_for_next_start: !selected && (!runningKnown || active), can_install: false, can_select: selected},
      {model_id: 'bird', version: 'aiy-v1', bundled: false, installed, valid: installed,
        selected, active: runningKnown && active,
        selected_for_next_start: selected && (!runningKnown || !active), can_install: !installed, can_select: installed && !selected},
    ],
  };
}
async function setup(page, initial = {}) {
  let saved = state(initial);
  await page.route(base, route => route.fulfill({json: saved}));
  await page.route(status, route => route.fulfill({json: {operation: null}}));
  await openAdmin(page, '/admin/config#classification');
  await expect(card(page).locator('.model-badges')).toContainText(initial.installed ? 'Installed' : 'Available to install');
  return next => { saved = state(next); return saved; };
}
test.beforeEach(async ({page}) => {
  page.__modelErrors = [];
  page.on('pageerror', error => page.__modelErrors.push(error.message));
});
test.afterEach(async ({page}) => { expect(page.__modelErrors).toEqual([]); });

test('catalogue, bundled active state, disclosures and mobile layout', async ({page}) => {
  await setup(page);
  const bundled = page.locator('[data-model-version="bundled-1"]');
  await expect(bundled).toContainText('Bird classification · bundled-1');
  await expect(bundled.locator('.model-badges')).toContainText('Bundled · Installed · Active');
  await expect(bundled.getByRole('button', {name: 'Install model'})).toHaveCount(0);
  await expect(card(page)).toContainText('Bird classification · aiy-v1');
  await card(page).getByText('Model details', {exact: true}).click();
  await expect(card(page)).toContainText('Apache-2.0');
  await expect(page.locator('#restart-btn')).toBeVisible();
  await noHorizontalOverflow(page);
});

test('install progress, keyboard action, duplicate suppression and unsaved field preservation', async ({page}) => {
  const update = await setup(page);
  let finish;
  const gate = new Promise(resolve => { finish = resolve; });
  let calls = 0;
  await page.route(install, async route => {
    calls++;
    expect(route.request().headers()['x-csrftoken']).toBeTruthy();
    await gate;
    update({installed: true});
    await route.fulfill({json: {success: true, code: 'installed', message: 'Model installed and validated. Selection has not changed.'}});
  });
  await page.route(status, route => route.fulfill({json: {operation: {
    model_id: 'bird', model_version: 'aiy-v1', phase: 'downloading', percent: 50,
    bytes_received: 50, bytes_total: 100, terminal_result: null,
  }}}));
  await page.locator('#classification_threshold').fill('0.88');
  const button = card(page).getByRole('button', {name: 'Install model'});
  await button.focus();
  await page.keyboard.press('Tab'); await page.keyboard.press('Shift+Tab');
  await visibleKeyboardFocus(button);
  await page.keyboard.press('Enter');
  await expect(button).toBeDisabled();
  await button.evaluate(element => element.dispatchEvent(new MouseEvent('click')));
  await expect.poll(() => calls).toBe(1);
  await expect(page.getByRole('progressbar', {name: 'Model installation'})).toBeVisible();
  await expect(page.locator('#model-message')).toContainText('50%');
  await page.locator('#classification_threshold').focus();
  finish();
  await expect(card(page).getByRole('button', {name: 'Use after restart'})).toBeVisible();
  await expect(page.locator('#classification_threshold')).toHaveValue('0.88');
  await expect(page.locator('#classification_threshold')).toBeFocused();
  await expect(page.locator('#model-selected')).toContainText('bundled-1');
  await noHorizontalOverflow(page);
});

test('selection saves only identity, preserves running state and clears restart notice after controlled restart', async ({page}) => {
  const update = await setup(page, {installed: true});
  await page.locator('#classification_threshold').fill('0.92');
  let calls = 0;
  await page.route(select, async route => {
    calls++;
    expect(route.request().postData()).toBeNull();
    await route.fulfill({json: {success: true, message: 'Model selection saved. Restart WAMF to apply it.',
      manager: update({installed: true, selected: true})}});
  });
  await card(page).getByRole('button', {name: 'Use after restart'}).click();
  await expect(page.locator('#model-running')).toContainText('bundled-1');
  await expect(page.locator('#model-selected')).toContainText('aiy-v1');
  await expect(page.locator('#model-restart-required')).toBeVisible();
  await expect(card(page).locator('.model-badges')).toContainText('Selected for next start');
  await expect(card(page).locator('.model-badges')).not.toContainText('Active');
  await expect(page.locator('#classification_threshold')).toHaveValue('0.92');
  await expect(page.locator('#classification_model')).toBeDisabled();
  expect(calls).toBe(1);
  // Controlled worker transition; no real restart or config mutation.
  const afterRestart = update({installed: true, selected: true, active: true});
  // Existing workers can serve the previous JSON shape until their next
  // ordinary restart; matching selected/active flags must still hide pending.
  afterRestart.models.forEach(model => { delete model.selected_for_next_start; });
  await page.reload();
  await expect(page.locator('#model-running')).toContainText('aiy-v1');
  await expect(card(page).locator('.model-badges')).toContainText('Active');
  await expect(page.locator('#classification')).not.toContainText('Selected for next start');
  await expect(page.locator('#model-restart-required')).toBeHidden();
  await noHorizontalOverflow(page);
});

test('selecting bundled while aiy-v1 runs marks only bundled for next start', async ({page}) => {
  const update = await setup(page, {installed: true, selected: true, active: true});
  const bundled = page.locator('[data-model-version="bundled-1"]');
  await page.route('**/admin/config/models/bird/bundled-1/select', route => route.fulfill({json: {
    success: true, message: 'Model selection saved. Restart WAMF to apply it.',
    manager: update({installed: true, selected: false, active: true}),
  }}));
  await bundled.getByRole('button', {name: 'Use after restart'}).click();
  await expect(bundled.locator('.model-badges')).toContainText('Selected for next start');
  await expect(bundled.locator('.model-badges')).not.toContainText('Active');
  await expect(card(page).locator('.model-badges')).toContainText('Active');
  await expect(card(page).locator('.model-badges')).not.toContainText('Selected for next start');
  await expect(page.locator('#model-running')).toContainText('aiy-v1');
  await expect(page.locator('#model-selected')).toContainText('bundled-1');
  await expect(page.locator('#model-restart-required')).toBeVisible();
});

for (const mode of ['unknown', 'stale']) {
  test(`${mode} running identity never implies Active or completed restart`, async ({page}) => {
    await setup(page, {installed: true, selected: true, active: true,
      known: mode !== 'unknown', stale: mode === 'stale'});
    await expect(page.locator('#model-running')).toContainText('Unknown');
    await expect(page.locator('[data-model-version="bundled-1"] .model-badges')).not.toContainText('Active');
    await expect(card(page).locator('.model-badges')).not.toContainText('Active');
    await expect(card(page).locator('.model-badges')).toContainText('Selected for next start');
    await expect(page.locator('#model-message')).not.toContainText('already running');
    await expect(page.locator('#model-restart-required')).toBeHidden();
  });
}

test('keyboard selection retains focus on its card when the action disappears', async ({page}) => {
  const update = await setup(page, {installed: true});
  await page.route(select, route => route.fulfill({json: {success: true,
    message: 'Model selection saved. Restart WAMF to apply it.', manager: update({installed: true, selected: true})}}));
  await card(page).getByRole('button', {name: 'Use after restart'}).focus();
  await page.keyboard.press('Enter');
  await expect(card(page)).toBeFocused();
  await expect(page.locator('#model-message')).toHaveAttribute('role', 'status');
});

for (const [code, message] of [
  ['checksum_mismatch', 'The artifact checksum does not match the catalogue.'],
  ['validation_failed', 'The model did not pass classifier validation.'],
  ['connection_error', 'The artifact server could not be reached.'],
]) {
  test(`safe ${code} failure with retry`, async ({page}) => {
    await setup(page);
    await page.route(install, route => route.fulfill({json: {success: false, code, message}}));
    await card(page).getByRole('button', {name: 'Install model'}).click();
    await expect(page.locator('#model-message')).toHaveText('Install failed: ' + message);
    await expect(page.locator('#model-message')).toHaveAttribute('role', 'alert');
    await expect(card(page).getByRole('button', {name: 'Install model'})).toBeEnabled();
    await expect(page.getByRole('progressbar')).toBeHidden();
  });
}

for (const action of ['install', 'select']) {
  for (const response of ['redirect', 'html']) {
    test(`${action} handles expired session (${response})`, async ({page}) => {
      await setup(page, {installed: action === 'select'});
      await page.route(action === 'install' ? install : select, route => response === 'redirect'
        ? route.fulfill({status: 302, headers: {Location: '/login'}})
        : route.fulfill({status: 200, contentType: 'text/html', body: '<h1>Log in</h1>'}));
      await card(page).getByRole('button', {name: action === 'install' ? 'Install model' : 'Use after restart'}).click();
      await expect(page.locator('#model-message')).toContainText('sign in again');
      await expect(page.locator('#model-message')).toHaveAttribute('role', 'alert');
      await expect(page.locator('#model-selected')).toContainText('bundled-1');
    });
  }
}

test('returning to Settings observes existing operation and recovers if progress was forgotten', async ({page}) => {
  await setup(page);
  let operation = {phase: 'validating', percent: 100, terminal_result: null};
  await page.route(status, route => route.fulfill({json: {operation}}));
  await page.reload();
  await expect(page.locator('#model-message')).toContainText('Validating');
  await expect(card(page).getByRole('button', {name: 'Install model'})).toBeDisabled();
  operation = null;
  await expect(card(page).getByRole('button', {name: 'Install model'})).toBeEnabled();
  await expect(page.getByRole('progressbar')).toBeHidden();
  await expect(page.locator('#model-message')).toContainText('No model installation is currently reported');
});
