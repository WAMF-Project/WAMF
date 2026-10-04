// Save only a manually authenticated browser session, never config secrets.
const { chromium } = require('@playwright/test');
const { mkdir, open } = require('node:fs/promises');
const { dirname } = require('node:path');
const readline = require('node:readline/promises');

(async () => {
  const target = process.env.WAMF_BROWSER_STORAGE_STATE || 'test-results/browser-auth/session.json';
  const browser = await chromium.launch({ headless: false });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(new URL('/admin', process.env.WAMF_BASE_URL || 'http://localhost:7767').href);
    const prompt = readline.createInterface({ input: process.stdin, output: process.stdout });
    try { await prompt.question('Log in in the browser, then press Enter here to save the session. '); }
    finally { prompt.close(); }
    await page.goto(new URL('/admin', page.url()).href);
    if (new URL(page.url()).pathname === '/login') throw new Error('Login has not completed.');
    await mkdir(dirname(target), { recursive: true, mode: 0o700 });
    const file = await open(target, 'w', 0o600);
    try {
      await file.chmod(0o600);
      await file.writeFile(JSON.stringify(await context.storageState()));
    } finally { await file.close(); }
    console.log(`Session saved to ${target}. Treat it as a credential.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
