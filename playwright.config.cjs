const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.spec.cjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: 'test-results/browser',
  use: {
    baseURL: process.env.WAMF_BASE_URL || 'http://localhost:7767',
    browserName: 'chromium',
    screenshot: 'only-on-failure',
    // Traces can contain Settings data and credentials; keep them disabled.
    trace: 'off',
    storageState: process.env.WAMF_BROWSER_STORAGE_STATE || undefined,
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'tablet', use: { viewport: { width: 820, height: 1180 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
